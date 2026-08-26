from __future__ import annotations

import re
from dataclasses import replace

from src.core import conditions as condition_service
from src.core import offer_structuring as structuring
from src.core.validity import extract_valid_until
from src.modules.source_registry import collectors
from src.modules.source_registry import service as registry_service
from src.modules.source_registry.proposal_apply import apply_assisted_proposal
from src.shared.logging import redact_secrets
from src.telegram import render as telegram_render
from src.web import customer_feedback_13


_PATCHED = False
_ORIGINAL_TELEGRAM_COLLECT = collectors.TelegramPublicCollector.collect
_ORIGINAL_RENDER = telegram_render.render_offer_caption

_AD_LINE_RE = re.compile(r"^(?:реклама\.?|erid\b|инн\b|рекламодатель\b)", re.IGNORECASE)
_URL_LINE_RE = re.compile(r"^https?://", re.IGNORECASE)


def _telegram_title(text: str) -> str | None:
    for raw_line in (text or "").splitlines():
        line = " ".join(raw_line.split()).strip(" •—–-\t")
        if len(line) < 4 or _AD_LINE_RE.match(line) or _URL_LINE_RE.match(line):
            continue
        if structuring._CTA_ONLY_RE.fullmatch(line):
            continue
        promo_candidates = structuring._promo_candidates(line)
        if promo_candidates and line.upper().strip(" :—-") in set(promo_candidates):
            continue
        return line[:240]
    return None


def _enrich_telegram_payload(payload: registry_service.ItemPayload) -> registry_service.ItemPayload:
    text = str(payload.text or "")
    metadata = dict(payload.raw_payload or {})
    promo_codes = structuring._promo_candidates(text)
    if len(promo_codes) == 1:
        metadata["promo_code"] = promo_codes[0]
    elif len(promo_codes) > 1:
        # Do not guess which code belongs to which offer. The universal
        # structurer will quarantine this post instead of publishing a merge.
        metadata["telegram_multiple_promo_codes"] = promo_codes

    signal = registry_service.detect_offer_signal(text)
    if metadata.get("discount_percent") in (None, "") and signal.discount_percent is not None:
        metadata["discount_percent"] = str(signal.discount_percent)
    if metadata.get("old_price") in (None, "") and signal.old_price is not None:
        metadata["old_price"] = str(signal.old_price)
    if metadata.get("new_price") in (None, "") and signal.new_price is not None:
        metadata["new_price"] = str(signal.new_price)

    extracted_conditions = condition_service.extract_conditions(payload.title, text)
    if metadata.get("conditions") in (None, "") and extracted_conditions.conditions:
        metadata["conditions"] = extracted_conditions.conditions
    if metadata.get("valid_until") in (None, ""):
        valid_until = extract_valid_until(text)
        if valid_until is not None:
            metadata["valid_until"] = valid_until.isoformat()

    title = _telegram_title(text) or payload.title
    metadata["telegram_structuring_version"] = "dp-cust-017"
    return replace(payload, title=title, raw_payload=metadata)


def _telegram_collect_v17(self, source):
    payloads = _ORIGINAL_TELEGRAM_COLLECT(self, source)
    return [_enrich_telegram_payload(payload) for payload in payloads]


def _render_offer_caption_v17(offer, publication_format=None) -> str:
    # Respect explicit user publication-format choices. DP-CUST-017 fixes the
    # missing promo at extraction/persistence time; it must not silently turn a
    # field back on when the user intentionally disabled it in format settings.
    caption = _ORIGINAL_RENDER(offer, publication_format)

    # Publication is an external boundary. Raw source text is not published by
    # design, and common named credentials/tokens are additionally redacted if
    # they accidentally appear in a customer-editable structured field.
    return redact_secrets(caption)


def _apply_existing_proposal_v17(proposal, *, source_id: int, name: str) -> None:
    apply_assisted_proposal(proposal, source_id=source_id, name=name)


def install_customer_feedback_17() -> None:
    global _PATCHED
    if _PATCHED:
        return
    collectors.TelegramPublicCollector.collect = _telegram_collect_v17
    telegram_render.render_offer_caption = _render_offer_caption_v17
    customer_feedback_13._apply_existing_proposal = _apply_existing_proposal_v17
    _PATCHED = True
