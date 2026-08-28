from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import func, select

from src.core.classification import classify_offer
from src.core.conditions import extract_conditions
from src.core.dedup import find_existing_offer
from src.core.geo import extract_geo
from src.core.normalization import NormalizedOffer, normalize_raw_offer
from src.modules.offers.models import Offer, OfferSourceObservation, ParseRun, Source
from src.modules.offers.repository import OfferRepository
from src.shared.db import session_scope
from src.sources.base import RawOffer
from src.sources.config import SourceConfig, load_source_configs
from src.sources.engine_runtime import collect_source_offers
from src.sources.registry import build_adapter


@dataclass(slots=True)
class RunResult:
    source_key: str
    fetched: int = 0
    created: int = 0
    updated: int = 0
    duplicates: int = 0
    errors: int = 0
    duration_seconds: float = 0.0
    error: str | None = None
    runtime_mode: str = "legacy"
    engine_discovered_urls: int = 0
    engine_selected_urls: int = 0
    engine_decoded_pages: int = 0
    engine_fallback_used: bool = False
    runtime_warnings: tuple[str, ...] = ()


def _ensure_source(session, config: SourceConfig) -> Source:
    source = session.scalar(select(Source).where(Source.key == config.key))
    if source is None:
        source = Source(key=config.key, name=config.name, base_url=config.base_url, enabled=config.enabled)
        session.add(source)
        session.flush()
    else:
        source.name = config.name
        source.base_url = config.base_url
    return source


def _source_is_enabled(config: SourceConfig) -> bool:
    with session_scope() as session:
        source = session.scalar(select(Source).where(Source.key == config.key))
        if source is None:
            return config.enabled
        return bool(source.enabled)


def _effective_config(config: SourceConfig) -> SourceConfig:
    """Overlay runtime registry network policy onto the legacy YAML adapter config.

    Registry is the operator-facing source of truth for route overrides. A local
    import avoids a module cycle because source_registry.runner imports this module.
    """
    try:
        from src.modules.source_registry.models import RegisteredSource

        with session_scope() as session:
            registered = session.scalar(select(RegisteredSource).where(RegisteredSource.key == config.key))
            if registered is not None and registered.network_policy:
                return replace(config, network_policy=registered.network_policy)
    except Exception:
        # Fresh/pre-registry databases and migration-time imports must keep the
        # legacy pipeline usable; YAML/default AUTO remains the fallback.
        pass
    return config


def _non_empty(values: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None and value != ""}


def _has_benefit(normalized: NormalizedOffer) -> bool:
    return any(
        value is not None
        for value in (
            normalized.discount_percent,
            normalized.discount_amount,
            normalized.cashback_percent,
            normalized.cashback_amount,
            normalized.delivery_price,
        )
    ) or bool(normalized.promo_code) or "бесплат" in normalized.title.lower()


def _raw_matches_geo(raw: RawOffer, *, city: str | None = None, region: str | None = None) -> bool:
    target_city = (city or "").strip().casefold()
    target_region = (region or "").strip().casefold()
    if not target_city and not target_region:
        return True
    geo = extract_geo(raw.title, raw.description, city=raw.city, region=raw.region, scope=raw.geo_scope)
    if target_city and (geo.city or "").casefold() != target_city:
        return False
    if target_region and (geo.region or "").casefold() != target_region:
        return False
    return True


def _normalized_values(raw: RawOffer, normalized: NormalizedOffer, now: datetime) -> dict[str, object]:
    geo = extract_geo(raw.title, raw.description, city=raw.city, region=raw.region, scope=raw.geo_scope)
    condition = extract_conditions(raw.title, raw.description, explicit=raw.conditions)
    return _non_empty(
        {
            "title": normalized.title,
            "description": raw.description,
            "merchant": normalized.merchant,
            "brand": normalized.brand,
            "geo_scope": geo.scope,
            "city": geo.city,
            "region": geo.region,
            "conditions": condition.conditions,
            "max_discount_amount": raw.max_discount_amount or condition.max_discount_amount,
            "min_order_amount": raw.min_order_amount or condition.min_order_amount,
            "promo_code": normalized.promo_code,
            "discount_percent": normalized.discount_percent,
            "discount_amount": normalized.discount_amount,
            "old_price": normalized.old_price,
            "new_price": normalized.new_price,
            "cashback_percent": normalized.cashback_percent,
            "cashback_amount": normalized.cashback_amount,
            "delivery_price": normalized.delivery_price,
            "canonical_url": normalized.canonical_url,
            "image_url": raw.image_url,
            "valid_from": raw.valid_from,
            "valid_until": raw.valid_until,
            "offer_type": normalized.offer_type,
            "fingerprint": normalized.fingerprint,
            "last_seen_at": now,
        }
    )


def _update_offer(session, offer: Offer, raw: RawOffer, normalized: NormalizedOffer, now: datetime) -> None:
    classification = classify_offer(
        session,
        title=normalized.title,
        merchant=normalized.merchant,
        brand=normalized.brand or offer.brand,
        offer=offer,
    )
    values: dict[str, object] = {
        **_normalized_values(raw, normalized, now),
        "category": classification.category,
        "subcategory": classification.subcategory,
    }
    if offer.status in {"new", "needs_review"} and _has_benefit(normalized) and classification.reason != "fallback":
        values["status"] = "ready"
    OfferRepository(session).update(offer, values)


def _persist_raw_offer(session, source: Source, raw: RawOffer) -> str:
    """Persist a raw offer, returning 'created', 'updated' or 'duplicate'."""
    now = datetime.now(UTC)
    normalized = normalize_raw_offer(raw)
    observation = session.scalar(
        select(OfferSourceObservation).where(
            OfferSourceObservation.source_id == source.id,
            OfferSourceObservation.external_id == raw.external_id,
        )
    )

    if observation is not None:
        _update_offer(session, observation.offer, raw, normalized, now)
        observation.observed_at = now
        observation.source_url = raw.source_url
        observation.raw_title = raw.title
        observation.raw_payload_json = json.dumps(
            {**(raw.raw_payload or {}), "dedup_reason": "source_external_id", "dedup_score": 100.0},
            ensure_ascii=False,
        )
        session.flush()
        return "updated"

    match = find_existing_offer(session, normalized)
    repo = OfferRepository(session)

    if match.offer is not None:
        offer = match.offer
        _update_offer(session, offer, raw, normalized, now)
    else:
        classification = classify_offer(session, title=normalized.title, merchant=normalized.merchant, brand=normalized.brand)
        status = "ready" if _has_benefit(normalized) and classification.reason != "fallback" else "needs_review"
        offer = repo.create(
            status=status,
            category=classification.category,
            subcategory=classification.subcategory,
            first_seen_at=now,
            **_normalized_values(raw, normalized, now),
        )

    session.add(
        OfferSourceObservation(
            offer_id=offer.id,
            source_id=source.id,
            external_id=raw.external_id,
            source_url=raw.source_url,
            raw_title=raw.title,
            raw_payload_json=json.dumps(
                {**(raw.raw_payload or {}), "dedup_reason": match.reason, "dedup_score": match.score},
                ensure_ascii=False,
            ),
            observed_at=now,
        )
    )
    session.flush()
    return "created" if match.offer is None else "duplicate"


def _record_failed_collection(config: SourceConfig, error: Exception) -> RunResult:
    message = f"{type(error).__name__}: {error}"
    with session_scope() as session:
        source = _ensure_source(session, config)
        session.add(ParseRun(source_id=source.id, status="failed", finished_at=datetime.now(UTC), error_count=1, error=message))
    return RunResult(source_key=config.key, errors=1, error=message, runtime_mode=config.runtime_mode)


def run_source(config: SourceConfig, *, city: str | None = None, region: str | None = None) -> RunResult:
    config = _effective_config(config)
    started = datetime.now(UTC)
    try:
        collection = collect_source_offers(config, adapter_factory=build_adapter)
    except Exception as exc:
        return _record_failed_collection(config, exc)

    raw_offers = [raw for raw in collection.offers if _raw_matches_geo(raw, city=city, region=region)]
    result = RunResult(
        source_key=config.key,
        fetched=len(raw_offers),
        runtime_mode=collection.runtime_mode,
        engine_discovered_urls=collection.discovered_urls,
        engine_selected_urls=collection.selected_urls,
        engine_decoded_pages=collection.decoded_pages,
        engine_fallback_used=collection.fallback_used,
        runtime_warnings=collection.warnings,
    )
    with session_scope() as session:
        source = _ensure_source(session, config)
        parse_run = ParseRun(source_id=source.id, status="running", fetched_count=result.fetched)
        session.add(parse_run)
        session.flush()

        errors: list[str] = []
        for raw in raw_offers:
            try:
                with session.begin_nested():
                    outcome = _persist_raw_offer(session, source, raw)
                if outcome == "created":
                    result.created += 1
                elif outcome == "updated":
                    result.updated += 1
                else:
                    result.duplicates += 1
            except Exception as exc:
                result.errors += 1
                errors.append(f"{raw.external_id}: {type(exc).__name__}: {exc}")

        result.duration_seconds = (datetime.now(UTC) - started).total_seconds()
        parse_run.new_count = result.created
        parse_run.updated_count = result.updated
        parse_run.duplicate_count = result.duplicates
        parse_run.review_count = int(
            session.scalar(
                select(func.count(func.distinct(Offer.id)))
                .join(OfferSourceObservation, OfferSourceObservation.offer_id == Offer.id)
                .where(
                    OfferSourceObservation.source_id == source.id,
                    OfferSourceObservation.observed_at >= parse_run.started_at,
                    Offer.status == "needs_review",
                )
            ) or 0
        )
        parse_run.error_count = result.errors
        parse_run.error = "\n".join(errors)[:10000] if errors else None
        parse_run.status = "partial" if errors else "success"
        parse_run.finished_at = datetime.now(UTC)
        result.error = parse_run.error
    return result


def run_all(
    path: str = "config/sources.yaml",
    only: str | None = None,
    *,
    city: str | None = None,
    region: str | None = None,
) -> list[RunResult]:
    results: list[RunResult] = []
    for config in load_source_configs(path):
        if not _source_is_enabled(config):
            continue
        if only and config.key != only:
            continue
        results.append(run_source(config, city=city, region=region))
    return results
