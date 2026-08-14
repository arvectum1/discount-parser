from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from src.modules.offers.models import Offer, OfferSourceObservation, Source
from src.modules.source_registry.collectors import CredentialsRequired, build_collector
from src.modules.source_registry.models import RegisteredSource, SourceKeyword
from src.modules.source_registry.service import detect_offer_signal, upsert_source_item
from src.shared.db import session_scope
from src.sources.base import RawOffer
from src.sources.runner import _persist_raw_offer
from src.core.validity import extract_valid_until
from src.modules.source_registry.promko_reveal import reveal_promko_code


@dataclass(slots=True)
class RegistryRunResult:
    source_key: str
    fetched: int = 0
    items_created: int = 0
    offer_signals: int = 0
    offers_created: int = 0
    offers_updated: int = 0
    duplicates: int = 0
    ignored: int = 0
    errors: int = 0
    duration_seconds: float = 0.0
    error: str | None = None

def _stored_promo_code(session, source: Source, external_id: str) -> str | None:
    offer = session.scalar(select(Offer).join(OfferSourceObservation).where(OfferSourceObservation.source_id == source.id, OfferSourceObservation.external_id == external_id))
    value = str(offer.promo_code or "").strip() if offer else ""
    return value or None

def _resolve_promko_code(session, *, source: RegisteredSource, legacy_source: Source, payload, item_created: bool, metadata: dict, result: RegistryRunResult) -> str | None:
    coupon_id = str(metadata.get("promko_coupon_id") or "").strip()
    explicit = str(metadata.get("promo_code") or "").strip()
    if not coupon_id or explicit: return explicit or None
    if not item_created:
        stored = _stored_promo_code(session, legacy_source, payload.external_id or "")
        if stored:
            metadata.update(promko_reveal_resolved=True, promko_reveal_reused=True, promo_code=stored)
            return stored
        message = f"PROMKO reveal {coupon_id}: unresolved; automatic retry disabled"
        metadata.update(promko_reveal_resolved=False, promko_reveal_reused=False, promko_reveal_error=message)
        result.errors += 1; result.error = message
        return None
    try:
        code = reveal_promko_code(coupon_id, referer=source.url, route=source.network_policy)
    except Exception as exc:
        message = f"PROMKO reveal {coupon_id}: {type(exc).__name__}: {exc}"
        metadata.update(promko_reveal_resolved=False, promko_reveal_reused=False, promko_reveal_error=message)
        result.errors += 1; result.error = message
        return None
    metadata.update(promo_code=code, promko_reveal_resolved=True, promko_reveal_reused=False, promko_reveal_error=None)
    return code


def _legacy_source(session, registered: RegisteredSource) -> Source:
    key = f"registry:{registered.key}"
    row = session.scalar(select(Source).where(Source.key == key))
    if row is None:
        row = Source(
            key=key,
            name=registered.name,
            kind=registered.platform,
            base_url=registered.url,
            enabled=registered.enabled,
        )
        session.add(row)
        session.flush()
    else:
        row.name = registered.name
        row.kind = registered.platform
        row.base_url = registered.url
        row.enabled = registered.enabled
    return row


def _keywords_for_source(session, source: RegisteredSource) -> list[SourceKeyword]:
    global_rows = session.scalars(
        select(SourceKeyword).where(SourceKeyword.enabled.is_(True), SourceKeyword.merchant.is_(None))
    ).all()
    merchant_rows: list[SourceKeyword] = []
    if source.merchant:
        merchant_rows = session.scalars(
            select(SourceKeyword).where(
                SourceKeyword.enabled.is_(True),
                SourceKeyword.merchant == source.merchant,
            )
        ).all()
    linked = [link.keyword for link in source.keyword_links if link.keyword.enabled]
    by_id = {row.id: row for row in [*global_rows, *merchant_rows, *linked] if row.id is not None}
    transient = [row for row in [*global_rows, *merchant_rows, *linked] if row.id is None]
    return [*by_id.values(), *transient]


def collect_registered_source(source_id: int) -> RegistryRunResult:
    started = datetime.now(UTC)
    with session_scope() as session:
        source = session.get(RegisteredSource, source_id)
        if source is None:
            raise KeyError(source_id)
        result = RegistryRunResult(source_key=source.key)
        if not source.enabled:
            source.status = "disabled"
            return result
        # Existing promo-code adapters continue through src.sources.runner. The
        # registry mirrors them for management/discovery without double-fetching.
        if source.collector_type == "legacy_adapter":
            return result
        collector_type = source.collector_type
        source_url = source.url

    try:
        collector = build_collector(collector_type)
        with session_scope() as session:
            source = session.get(RegisteredSource, source_id)
            assert source is not None
            payloads = collector.collect(source)
    except CredentialsRequired as exc:
        with session_scope() as session:
            source = session.get(RegisteredSource, source_id)
            assert source is not None
            now = datetime.now(UTC)
            source.status = "requires_credentials"
            source.last_checked_at = now
            source.last_error_at = now
            source.last_error = str(exc)
            source.failure_count += 1
        result.errors = 1
        result.error = str(exc)
        return result
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        with session_scope() as session:
            source = session.get(RegisteredSource, source_id)
            assert source is not None
            now = datetime.now(UTC)
            source.status = "blocked" if "403" in message else "degraded"
            source.last_checked_at = now
            source.last_error_at = now
            source.last_error = message[:4000]
            source.failure_count += 1
        result.errors = 1
        result.error = message
        return result

    result.fetched = len(payloads)
    with session_scope() as session:
        source = session.get(RegisteredSource, source_id)
        assert source is not None
        legacy_source = _legacy_source(session, source)
        keywords = _keywords_for_source(session, source)
        for payload in payloads:
            item = None
            try:
                item, created = upsert_source_item(session, source, payload)
                result.items_created += int(created)
                metadata = dict(payload.raw_payload or {})
                combined_text = "\n".join(part for part in (payload.title, payload.text) if part)
                valid_until = extract_valid_until(str(metadata.get("valid_until") or "")) or extract_valid_until(combined_text)
                if valid_until is not None and valid_until < datetime.now(UTC):
                    item.processing_status = "ignored"; item.raw_payload_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
                    result.ignored += 1
                    continue
                signal = detect_offer_signal(combined_text, keywords)
                if not signal.is_offer:
                    item.processing_status = "ignored"
                    item.raw_payload_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
                    result.ignored += 1
                    continue
                result.offer_signals += 1
                promo_code = _resolve_promko_code(session, source=source, legacy_source=legacy_source, payload=payload, item_created=created, metadata=metadata, result=result) or str(metadata.get("promo_code") or "").strip() or signal.promo_code
                item.raw_payload_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
                title = (payload.title or payload.text or "Предложение").strip().splitlines()[0][:1000]
                raw = RawOffer(
                    source_key=legacy_source.key,
                    external_id=payload.external_id or item.content_hash,
                    title=title,
                    source_url=payload.url or source_url,
                    merchant=source.merchant,
                    brand=source.brand,
                    description=payload.text,
                    conditions=str(metadata.get("conditions") or "").strip() or None,
                    promo_code=promo_code,
                    discount_percent=signal.discount_percent,
                    old_price=signal.old_price,
                    new_price=signal.new_price,
                    image_url=payload.image_url,
                    valid_from=payload.published_at,
                    valid_until=valid_until,
                    raw_payload={
                        "registered_source_id": source.id,
                        "source_item_id": item.id,
                        "platform": source.platform,
                        "matched_keywords": list(signal.matched_keywords),
                        "signal_confidence": signal.confidence,
                        "source_item_payload": metadata,
                    },
                )
                created_offer = _persist_raw_offer(session, legacy_source, raw)
                if created_offer == "created":
                    result.offers_created += 1
                elif created_offer == "updated":
                    result.offers_updated += 1
                else:
                    result.duplicates += 1
                item.processing_status = "processed"
                item.processing_error = None
            except Exception as exc:
                result.errors += 1
                result.error = f"{type(exc).__name__}: {exc}"
                if item is not None:
                    item.processing_status = "failed"
                    item.processing_error = result.error[:4000]

        result.duration_seconds = (datetime.now(UTC) - started).total_seconds()
        now = datetime.now(UTC)
        source.last_checked_at = now
        if result.errors:
            source.status = "degraded"
            source.last_error_at = now
            source.last_error = result.error
            source.failure_count += 1
        else:
            source.status = "healthy"
            source.last_success_at = now
            source.last_error = None
            source.failure_count = 0
    return result


def collect_registered_sources(*, only_key: str | None = None) -> list[RegistryRunResult]:
    with session_scope() as session:
        statement = select(RegisteredSource.id).where(
            RegisteredSource.enabled.is_(True),
            RegisteredSource.collector_type != "legacy_adapter",
        )
        if only_key:
            statement = statement.where(RegisteredSource.key == only_key)
        source_ids = list(session.scalars(statement).all())
    return [collect_registered_source(source_id) for source_id in source_ids]
