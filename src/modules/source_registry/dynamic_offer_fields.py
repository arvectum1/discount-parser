from __future__ import annotations

from dataclasses import replace

from src.sources.base import RawOffer


_PATCH_MARKER = "_dp_cust_011_dynamic_offer_fields"
_QUALITY_MARKER = "_dp_cust_015_quality_gate"


def _merchant_from_payload(raw: RawOffer) -> str | None:
    payload = dict(raw.raw_payload or {})
    nested = payload.get("source_item_payload")
    if isinstance(nested, dict):
        value = str(nested.get("merchant") or "").strip()
        if value:
            return value[:255]
    value = str(payload.get("merchant") or "").strip()
    return value[:255] or None


def install_dynamic_offer_fields() -> None:
    """Let multi-merchant registry sources carry merchant per detail page.

    The wrapper deliberately preserves the DP-CUST-015 quality-gate marker when
    it wraps the universal persistence gate. This makes patch composition
    explicit: dynamic merchant enrichment may run before persistence, but it
    must never bypass the universal offer-quality checks.
    """
    from src.modules.source_registry import runner as registry_runner
    from src.sources import runner as legacy_runner

    current = registry_runner._persist_raw_offer
    if getattr(current, _PATCH_MARKER, False):
        return

    def persist_with_dynamic_fields(session, source, raw: RawOffer) -> str:
        merchant = _merchant_from_payload(raw)
        if merchant and not raw.merchant:
            raw = replace(raw, merchant=merchant)
        return current(session, source, raw)

    setattr(persist_with_dynamic_fields, _PATCH_MARKER, True)
    if getattr(current, _QUALITY_MARKER, False):
        setattr(persist_with_dynamic_fields, _QUALITY_MARKER, True)
    registry_runner._persist_raw_offer = persist_with_dynamic_fields
    # Keep the module-level helper coherent for any later imports/callers.
    legacy_runner._persist_raw_offer = persist_with_dynamic_fields
