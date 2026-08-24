from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from src.core import offer_structuring as core
from src.sources.base import RawOffer


READINESS_VERSION = "dp-cust-016"
AUTO_READY_THRESHOLD = 0.70
BLOCKING_ISSUES = frozenset(
    {
        "multiple_promo_codes",
        "multiple_offer_markers",
        "multiple_discount_percentages",
        "multiple_prices",
        "multiple_validity_markers",
        "invalid_title",
        "invalid_structured_promo",
        "benefit_needs_review",
    }
)

_PATCHED = False
_ORIGINAL_STRUCTURE_RAW = core.structure_raw_offer


def _has_concrete_benefit(raw: RawOffer) -> bool:
    return any(
        value is not None
        for value in (
            raw.promo_code,
            raw.discount_percent,
            raw.discount_amount,
            raw.old_price,
            raw.new_price,
            raw.cashback_percent,
            raw.cashback_amount,
            raw.delivery_price,
        )
    )


def _auto_ready(result: core.StructuredOffer) -> bool:
    if not result.accepted:
        return False
    if not _has_concrete_benefit(result.raw):
        return False
    if BLOCKING_ISSUES.intersection(result.issues):
        return False
    return result.confidence >= AUTO_READY_THRESHOLD


def structure_raw_offer_calibrated(raw: RawOffer) -> core.StructuredOffer:
    """Keep DP-CUST-015 precision while preventing blanket manual review.

    DP-CUST-015 correctly quarantines merged or incoherent content, but its
    original auto-ready threshold required merchant/conditions on many generic
    sources. Those are useful enrichment fields, not safety-critical evidence.

    A coherent offer with a meaningful title, a concrete benefit and a valid
    source/offer URL reaches 0.73 under the existing quality model and is now
    allowed through automatically. Ambiguity issues remain hard blockers.
    """
    result = _ORIGINAL_STRUCTURE_RAW(raw)
    ready = _auto_ready(result)
    payload = dict(result.raw.raw_payload or {})
    payload.update(
        {
            "readiness_version": READINESS_VERSION,
            "readiness_threshold": AUTO_READY_THRESHOLD,
            "readiness_blocking_issues": sorted(BLOCKING_ISSUES.intersection(result.issues)),
            "structuring_auto_ready": ready,
        }
    )
    calibrated_raw = replace(result.raw, raw_payload=payload)
    return replace(result, raw=calibrated_raw, auto_ready=ready)


def install_customer_feedback_16() -> None:
    global _PATCHED
    if _PATCHED:
        return

    # structure_registry_payload resolves structure_raw_offer from the core
    # module at call time, so replacing this one function calibrates every
    # registry collector without duplicating DP-CUST-015 parsing logic.
    core.structure_raw_offer = structure_raw_offer_calibrated

    # DP-CUST-015 imported structure_raw_offer directly for legacy-adapter
    # persistence. Keep that path on exactly the same readiness policy.
    from src import customer_feedback_15

    customer_feedback_15.structure_raw_offer = structure_raw_offer_calibrated
    _PATCHED = True
