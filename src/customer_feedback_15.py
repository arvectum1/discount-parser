from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select

from src.core.offer_structuring import (
    StructuringBatch,
    is_expired,
    structure_raw_offer,
    structure_registry_payload,
)
from src.modules.offers.models import OfferSourceObservation
from src.modules.source_registry import runner as registry_runner
from src.sources import runner as sources_runner


_PATCHED = False
_ORIGINAL_PERSIST = sources_runner._persist_raw_offer
_GENERIC_TITLES = {"предложение", "акция", "скидка", "промокод", "спецпредложение"}


def _seed_title(payload) -> str | None:
    if payload.title and str(payload.title).strip().casefold() not in _GENERIC_TITLES:
        return str(payload.title).strip()
    for raw_line in str(payload.text or "").splitlines():
        line = " ".join(raw_line.split()).strip(" •\t")
        if not line or line.casefold().strip(" .:-—") in _GENERIC_TITLES:
            continue
        return line[:1000]
    return payload.title


def _persist_with_quality_gate(session, source, raw):
    """Persist one coherent offer and never auto-promote low-confidence parsing."""
    structured = structure_raw_offer(raw)
    outcome = _ORIGINAL_PERSIST(session, source, structured.raw)

    observation = session.scalar(
        select(OfferSourceObservation).where(
            OfferSourceObservation.source_id == source.id,
            OfferSourceObservation.external_id == structured.raw.external_id,
        )
    )
    if observation is not None:
        offer = observation.offer
        # A weak parser result may update provenance, but it must never become
        # publishable automatically. Published/rejected states are historical
        # operator decisions and are deliberately left untouched.
        if not structured.auto_ready and offer.status in {"new", "ready", "needs_review"}:
            offer.status = "needs_review"
        session.flush()
    return outcome


def _review_message(batch: StructuringBatch) -> str:
    return batch.reason or "Предложение требует проверки перед публикацией."


def _collect_registered_source_v15(source_id: int):
    started = datetime.now(UTC)
    with registry_runner.session_scope() as session:
        source = session.get(registry_runner.RegisteredSource, source_id)
        if source is None:
            raise KeyError(source_id)
        result = registry_runner.RegistryRunResult(source_key=source.key)
        if not source.enabled:
            source.status = "disabled"
            return result
        if source.collector_type == "legacy_adapter":
            return result
        collector_type = source.collector_type
        source_url = source.url

    try:
        collector = registry_runner.build_collector(collector_type)
        with registry_runner.session_scope() as session:
            source = session.get(registry_runner.RegisteredSource, source_id)
            assert source is not None
            payloads = collector.collect(source)
    except registry_runner.CredentialsRequired as exc:
        with registry_runner.session_scope() as session:
            source = session.get(registry_runner.RegisteredSource, source_id)
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
        with registry_runner.session_scope() as session:
            source = session.get(registry_runner.RegisteredSource, source_id)
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
    with registry_runner.session_scope() as session:
        source = session.get(registry_runner.RegisteredSource, source_id)
        assert source is not None
        legacy_source = registry_runner._legacy_source(session, source)
        keywords = registry_runner._keywords_for_source(session, source)

        for payload in payloads:
            item = None
            try:
                item, created = registry_runner.upsert_source_item(session, source, payload)
                result.items_created += int(created)
                metadata = dict(payload.raw_payload or {})
                combined_text = "\n".join(part for part in (payload.title, payload.text) if part)
                signal = registry_runner.detect_offer_signal(combined_text, keywords)

                # Promko reveal remains source-specific transport logic. The
                # resulting code then enters the same universal field contract
                # as every other source.
                resolved = registry_runner._resolve_promko_code(
                    session,
                    source=source,
                    legacy_source=legacy_source,
                    payload=payload,
                    item_created=created,
                    metadata=metadata,
                    result=result,
                )
                if resolved:
                    metadata["promo_code"] = resolved
                payload_for_structuring = replace(
                    payload,
                    title=_seed_title(payload),
                    raw_payload=metadata,
                )

                batch = structure_registry_payload(
                    payload_for_structuring,
                    source_key=legacy_source.key,
                    source_url=source_url,
                    source_merchant=source.merchant,
                    source_brand=source.brand,
                    platform=source.platform,
                    signal=signal,
                )

                if batch.disposition == "ignored":
                    item.processing_status = "ignored"
                    item.processing_error = None
                    item.raw_payload_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
                    result.ignored += 1
                    continue

                if not batch.candidates:
                    # Critical safety behaviour: do not create an Offer from a
                    # merged/ambiguous block. Keep the source item for evidence
                    # and make it visible as review-required instead.
                    item.processing_status = "needs_review"
                    item.processing_error = _review_message(batch)[:4000]
                    metadata.update(
                        {
                            "structuring_version": "dp-cust-015",
                            "structuring_accepted": False,
                            "structuring_review_reason": item.processing_error,
                        }
                    )
                    item.raw_payload_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
                    result.offer_signals += 1
                    continue

                result.offer_signals += 1
                candidate_review = batch.disposition == "needs_review"
                persisted_any = False
                for index, candidate in enumerate(batch.candidates, start=1):
                    raw = candidate.raw
                    if is_expired(raw.valid_until):
                        continue
                    external_id = raw.external_id or item.content_hash
                    if len(batch.candidates) > 1:
                        external_id = f"{external_id}:part{index}"
                    raw = replace(
                        raw,
                        external_id=external_id,
                        raw_payload={
                            **(raw.raw_payload or {}),
                            "registered_source_id": source.id,
                            "source_item_id": item.id,
                            "platform": source.platform,
                            "matched_keywords": list(getattr(signal, "matched_keywords", ()) or ()),
                            "signal_confidence": int(getattr(signal, "confidence", 0) or 0),
                            "source_item_payload": metadata,
                        },
                    )
                    outcome = _persist_with_quality_gate(session, legacy_source, raw)
                    persisted_any = True
                    if outcome == "created":
                        result.offers_created += 1
                    elif outcome == "updated":
                        result.offers_updated += 1
                    else:
                        result.duplicates += 1
                    candidate_review = candidate_review or not candidate.auto_ready

                if not persisted_any:
                    item.processing_status = "ignored"
                    item.processing_error = None
                    result.ignored += 1
                    continue

                item.processing_status = "needs_review" if candidate_review else "processed"
                item.processing_error = _review_message(batch)[:4000] if candidate_review else None
                candidate_payload = batch.candidates[0].raw.raw_payload or metadata
                item.raw_payload_json = json.dumps(candidate_payload, ensure_ascii=False, sort_keys=True)
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


def install_customer_feedback_15() -> None:
    global _PATCHED
    if _PATCHED:
        return

    # Legacy adapters and registry collectors now meet at the same persistence
    # quality gate. Registry collection additionally refuses to persist merged
    # or incoherent source items as offers.
    sources_runner._persist_raw_offer = _persist_with_quality_gate
    registry_runner._persist_raw_offer = _persist_with_quality_gate
    registry_runner.collect_registered_source = _collect_registered_source_v15
    _PATCHED = True
