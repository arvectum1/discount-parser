from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Sequence
from urllib.parse import parse_qs, urljoin, urlsplit

from arvectum_data.engine import (
    Candidate,
    Evidence,
    FieldSpec,
    MultiRecordExtractionEngine,
    RawAsset,
    RecordSetResult,
    RecordStatus,
)
from arvectum_data.engine.html_records import SemanticHTMLRecordProvider
from src.core.validity import extract_valid_until
from src.sources.adapters.common import external_id, parse_amount, parse_percent
from src.sources.base import RawOffer

_ACTION_SUFFIX_RE = re.compile(
    r"\s+(?:открыть|показать|активировать|получить|применить|использовать|скопировать)\s+"
    r"(?:промокод\w*|код\b|акци\w*).*$",
    re.IGNORECASE,
)
_ACTION_ACTIVATE_RE = re.compile(
    r"(?:активировать|получить|применить|использовать)\s+промокод",
    re.IGNORECASE,
)
_ACTION_OPEN_RE = re.compile(r"(?:открыть|open)\s+(?:промокод|акци\w*|coupon|promo|deal)", re.IGNORECASE)
_ACTION_SHOW_RE = re.compile(r"(?:показать|show|reveal)\s+(?:промокод|акци\w*|coupon|promo|deal)", re.IGNORECASE)
_REVEAL_ACTION_RE = re.compile(r"(?:открыть|показать|open|show|reveal)\s+(?:промокод|акци\w*|coupon|promo|deal)", re.IGNORECASE)
_BENEFIT_RE = re.compile(
    r"\b(?:доп\.?\s*)?(?:скидк\w*|промокод\w*|бонус\w*|к[еэ]шб\w*|бесплат\w*|подар\w*|сертификат\w*)\b",
    re.IGNORECASE,
)
_SUMMARY_RE = re.compile(r"^(.+?)\s*до\s*(\d{1,3})\s*%$", re.IGNORECASE)
_MERCHANT_FROM_STRONG_RE = re.compile(r"(?:^|\s)(?:от|для)\s+(.+)$", re.IGNORECASE)
_CODE_AFTER_LABEL_RE = re.compile(
    r"(?:промокод|код)\s*[:\-–—]?\s*([A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9_-]{3,24})",
    re.IGNORECASE,
)
_CODE_TOKEN_RE = re.compile(r"^[A-ZА-ЯЁ0-9_-]{4,24}$")
_CODE_SCAN_RE = re.compile(
    r"\b(?=[A-ZА-ЯЁ0-9_-]{4,24}\b)(?=[A-ZА-ЯЁ0-9_-]*\d|[A-ZА-ЯЁ0-9_-]{5,})([A-ZА-ЯЁ0-9_-]+)\b"
)
_STOP_CODES = {"IMAGE", "КОД", "ПРОМОКОД", "ПРОМОКОДЫ", "COUPON", "PROMO"}

OFFER_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("external_id", required=True, min_confidence=0.90),
    FieldSpec("title", required=True, min_confidence=0.90),
    FieldSpec("source_url", required=True, min_confidence=0.90),
    FieldSpec("merchant", min_confidence=0.85),
    FieldSpec("description", min_confidence=0.85),
    FieldSpec("conditions", min_confidence=0.85),
    FieldSpec("promo_code", min_confidence=0.85),
    FieldSpec("discount_percent", min_confidence=0.85),
    FieldSpec("discount_amount", min_confidence=0.85),
    FieldSpec("cashback_percent", min_confidence=0.85),
    FieldSpec("image_url", min_confidence=0.85),
    FieldSpec("valid_until", min_confidence=0.85),
)

_PARITY_FIELDS = (
    "title",
    "source_url",
    "merchant",
    "description",
    "conditions",
    "promo_code",
    "discount_percent",
    "discount_amount",
    "cashback_percent",
    "image_url",
    "valid_until",
)


def _compact(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _candidate(field_key: str, value: Any, *, source_ref: str, confidence: float = 0.98) -> Candidate:
    return Candidate(
        field_key=field_key,
        value=value,
        confidence=confidence,
        provider="discount_offer_semantics",
        evidence=(
            Evidence(
                kind="semantic_record_field",
                source_ref=source_ref,
                excerpt=str(value)[:500],
            ),
        ),
    )


class DiscountOfferCandidateProvider:
    """Interpret one generic record slice as Discount Parser business fields.

    Rules describe promotion semantics, not host names or site selectors. The
    structural record provider remains domain-neutral; this provider is the
    application-domain bridge from generic records to ``RawOffer`` fields.
    """

    name = "discount_offer_semantics"

    def candidates(self, asset: RawAsset, fields: Sequence[FieldSpec]) -> Sequence[Candidate]:
        requested = {field.key for field in fields}
        attrs = dict(asset.attributes)
        text = _compact(str(attrs.get("record_text") or asset.text or ""))
        if not text:
            return ()

        heading = _compact(attrs.get("record_heading")) or None
        strong = _compact(attrs.get("record_strong")) or None
        record_href = _compact(attrs.get("record_href")) or None
        action_href = _compact(attrs.get("record_action_href")) or None
        action_text = _compact(attrs.get("record_action_text")) or None
        anchor_kind = _compact(attrs.get("record_anchor_kind")).casefold()
        if anchor_kind == "action":
            href = action_href
        elif anchor_kind == "heading":
            href = action_href or record_href
        else:
            href = record_href
        image_src = _compact(attrs.get("record_image_src")) or None
        image_alt = _compact(attrs.get("record_image_alt")) or None
        record_tag = _compact(attrs.get("record_tag")).casefold()
        data = dict(attrs.get("record_data") or {})
        source_ref = str(asset.metadata.get("record_source_ref") or asset.asset_id)
        source_key = _compact(str(asset.metadata.get("source_key") or "generic")) or "generic"
        base_url = asset.source_url or ""

        summary = _SUMMARY_RE.fullmatch(text)
        prefer_image_merchant = bool(
            (action_href and "offer_id=" in action_href)
            or (action_text and _ACTION_SHOW_RE.search(action_text))
        )
        merchant = self._merchant(
            text,
            heading,
            strong,
            image_alt,
            summary,
            action_text=action_text,
            prefer_image=prefer_image_merchant,
        )
        title = self._title(text, heading, merchant, summary)
        source_url = urljoin(base_url, href) if href else base_url
        promo_code = self._promo_code(
            text,
            heading,
            strong,
            data,
            suppress_inference=bool(action_text and _REVEAL_ACTION_RE.search(action_text)),
        )

        percent = parse_percent(title) or parse_percent(text)
        cashback_percent: Decimal | None = None
        if percent is not None and re.search(r"к[еэ]шб|cashback", title or text, re.IGNORECASE):
            cashback_percent = percent
            percent = None
        amount = None if percent is not None or cashback_percent is not None else parse_amount(title) or parse_amount(text)
        image_url = urljoin(base_url, image_src) if image_src else None
        valid_until = extract_valid_until(text)
        conditions = text if _ACTION_ACTIVATE_RE.search(text) else None
        ext_id = self._external_id(
            source_key=source_key,
            source_url=source_url,
            title=title,
            merchant=merchant,
            promo_code=promo_code,
            percent=percent,
            record_tag=record_tag,
            anchor_kind=anchor_kind,
            action_text=action_text,
            text=text,
            data=data,
        )

        values: dict[str, Any] = {
            "external_id": ext_id,
            "title": title,
            "source_url": source_url,
            "merchant": merchant,
            "description": text[:2000],
            "conditions": conditions[:2000] if conditions else None,
            "promo_code": promo_code,
            "discount_percent": percent,
            "discount_amount": amount,
            "cashback_percent": cashback_percent,
            "image_url": image_url,
            "valid_until": valid_until,
        }
        return tuple(
            _candidate(key, value, source_ref=source_ref)
            for key, value in values.items()
            if key in requested and value is not None and value != ""
        )

    @staticmethod
    def _merchant(
        text: str,
        heading: str | None,
        strong: str | None,
        image_alt: str | None,
        summary: re.Match[str] | None,
        *,
        action_text: str | None,
        prefer_image: bool,
    ) -> str | None:
        if strong:
            match = _MERCHANT_FROM_STRONG_RE.search(strong)
            if match:
                value = match.group(1).strip(" .:-—")
                if value:
                    return value[:120]
            if (
                len(strong) <= 120
                and not _BENEFIT_RE.search(strong)
                and not _CODE_TOKEN_RE.fullmatch(strong)
                and strong.casefold() != (action_text or "").casefold()
            ):
                return strong
        if (
            heading
            and len(heading) <= 120
            and not _BENEFIT_RE.search(heading)
            and heading.casefold() != (action_text or "").casefold()
        ):
            return heading
        if prefer_image and image_alt and len(image_alt) <= 120:
            return image_alt
        if summary:
            value = summary.group(1).strip(" .:-—")
            return value[:120] or None
        patterns = (
            r"\b(?:от|для|в)\s+([A-Za-zА-Яа-яЁё0-9. -]{2,40}?)(?:\s+на\s+|\s+по\s+|\s+-?\d|$)",
            r"^Промокод\s+([A-Za-zА-Яа-яЁё0-9. -]{2,40}?)\s+(?:июл|август|сент|на)",
            r"\bот\s+([A-Za-zА-Яа-яЁё0-9. -]{2,50})(?:$|[,.!])",
        )
        for pattern in patterns:
            match = re.search(pattern, heading or text, re.IGNORECASE)
            if match:
                value = match.group(1).strip(" .:-—")
                if value:
                    return value[:120]
        benefit = _BENEFIT_RE.search(text)
        if benefit:
            prefix = text[: benefit.start()].strip(" .:-—")
            if prefix and len(prefix) <= 120 and prefix.casefold() != (heading or "").casefold():
                return prefix
        return None

    @staticmethod
    def _title(
        text: str,
        heading: str | None,
        merchant: str | None,
        summary: re.Match[str] | None,
    ) -> str:
        if heading and _BENEFIT_RE.search(heading):
            return heading[:300]
        if summary and merchant:
            return f"Скидка до {int(summary.group(2))}% в {merchant}"[:300]
        value = text
        if merchant and value.casefold().startswith(merchant.casefold()):
            value = value[len(merchant):].strip(" .:-—")
        value = _ACTION_SUFFIX_RE.sub("", value).strip(" .:-—")
        return (value[:300] or merchant or "Предложение")

    @staticmethod
    def _promo_code(
        text: str,
        heading: str | None,
        strong: str | None,
        data: dict[str, Any],
        *,
        suppress_inference: bool,
    ) -> str | None:
        for key in ("data-promocode", "data-promo-code"):
            value = _compact(str(data.get(key) or ""))
            if value and not re.fullmatch(r"[•*\s]+", value):
                return value[:120]
        if suppress_inference:
            return None
        if strong and _CODE_TOKEN_RE.fullmatch(strong) and strong.upper() not in _STOP_CODES:
            return strong
        tail = text
        if heading and text.casefold().startswith(heading.casefold()):
            tail = text[len(heading):]
        match = _CODE_AFTER_LABEL_RE.search(tail)
        if match:
            value = match.group(1)
            if value.upper() not in _STOP_CODES:
                return value
        for match in _CODE_SCAN_RE.finditer(tail):
            value = match.group(1)
            if value.upper() not in _STOP_CODES:
                return value
        return None

    @staticmethod
    def _external_id(
        *,
        source_key: str,
        source_url: str,
        title: str,
        merchant: str | None,
        promo_code: str | None,
        percent: Decimal | None,
        record_tag: str,
        anchor_kind: str,
        action_text: str | None,
        text: str,
        data: dict[str, Any],
    ) -> str:
        coupon_id = _compact(str(data.get("data-coupon-id") or ""))
        if coupon_id.isdigit():
            return f"{source_key}-coupon:{coupon_id}"
        offer_id = parse_qs(urlsplit(source_url).query).get("offer_id", [None])[0]
        if offer_id:
            return str(offer_id)
        summary = _SUMMARY_RE.fullmatch(text)
        if summary and merchant and percent is not None:
            return external_id(source_url, merchant, str(percent))
        if anchor_kind == "heading":
            return external_id(source_url, title, promo_code)
        action_signal = action_text or text
        if _ACTION_ACTIVATE_RE.search(action_signal):
            return external_id(source_url, merchant, title, promo_code or "")
        if _ACTION_SHOW_RE.search(action_signal):
            return external_id(source_url, title)
        if _ACTION_OPEN_RE.search(action_signal):
            return external_id(source_url, merchant, title)
        if promo_code or record_tag == "article":
            return external_id(source_url, title, promo_code)
        return external_id(source_url, merchant, title)


@dataclass(frozen=True, slots=True)
class GenericOfferDecodeResult:
    offers: tuple[RawOffer, ...]
    records: RecordSetResult
    usable: bool
    warnings: tuple[str, ...] = ()


class GenericMultiRecordOfferDecoder:
    def __init__(self) -> None:
        self.engine = MultiRecordExtractionEngine(
            (SemanticHTMLRecordProvider(),),
            (DiscountOfferCandidateProvider(),),
            min_boundary_confidence=0.80,
        )

    def decode(self, html: str, *, page_url: str, source_key: str) -> GenericOfferDecodeResult:
        digest = hashlib.sha256(f"{source_key}|{page_url}".encode("utf-8")).hexdigest()[:24]
        asset = RawAsset(
            asset_id=f"source-page:{digest}",
            source_url=page_url,
            html=html,
            metadata={"source_key": source_key, "page_url": page_url},
        )
        result = self.engine.extract(asset, OFFER_FIELDS)
        warnings: list[str] = []
        warnings.extend(
            f"record_provider:{provider}:{message}"
            for provider, message in sorted(result.record_provider_errors.items())
        )
        for provider, messages in sorted(result.record_provider_warnings.items()):
            warnings.extend(f"record_provider:{provider}:{message}" for message in messages)

        ready_records = tuple(record for record in result.records if record.status is RecordStatus.READY)
        usable = bool(ready_records)
        offers: list[RawOffer] = []
        seen_external_ids: set[str] = set()
        for record in ready_records:
            values = record.values()
            external_id_value = str(values["external_id"])
            if external_id_value in seen_external_ids:
                warnings.append(f"duplicate_record_identity:{record.record_id}")
                continue
            seen_external_ids.add(external_id_value)
            offers.append(
                RawOffer(
                    source_key=source_key,
                    external_id=external_id_value,
                    title=str(values["title"]),
                    source_url=str(values["source_url"]),
                    merchant=values.get("merchant"),
                    description=values.get("description"),
                    conditions=values.get("conditions"),
                    promo_code=values.get("promo_code"),
                    discount_percent=values.get("discount_percent"),
                    discount_amount=values.get("discount_amount"),
                    cashback_percent=values.get("cashback_percent"),
                    image_url=values.get("image_url"),
                    valid_until=values.get("valid_until"),
                    raw_payload={
                        "text": values.get("description"),
                        "dp_engine": {
                            "decoder": "generic_multi_record",
                            "record_id": record.record_id,
                            "record_provider": record.boundary.provider,
                            "record_source_ref": record.boundary.source_ref,
                        },
                    },
                )
            )
        for record in result.records:
            if record.status is not RecordStatus.READY:
                warnings.append(f"record_not_ready:{record.record_id}:{record.status.value}")
        return GenericOfferDecodeResult(
            offers=tuple(offers),
            records=result,
            usable=usable,
            warnings=tuple(warnings),
        )


@dataclass(frozen=True, slots=True)
class ParityMismatch:
    external_id: str
    field: str
    legacy_value: str
    generic_value: str


@dataclass(frozen=True, slots=True)
class SourceParityReport:
    safe_to_adopt: bool
    legacy_count: int
    generic_count: int
    matched_count: int
    missing_ids: tuple[str, ...] = ()
    extra_ids: tuple[str, ...] = ()
    mismatches: tuple[ParityMismatch, ...] = ()

    def diagnostic(self, *, max_items: int = 4) -> str:
        parts = [
            f"legacy={self.legacy_count}",
            f"generic={self.generic_count}",
            f"matched={self.matched_count}",
        ]
        if self.missing_ids:
            parts.append("missing=" + ",".join(self.missing_ids[:max_items]))
        if self.extra_ids:
            parts.append("extra=" + ",".join(self.extra_ids[:max_items]))
        if self.mismatches:
            sample = self.mismatches[:max_items]
            parts.append("mismatch=" + ",".join(f"{item.external_id}:{item.field}" for item in sample))
        return ";".join(parts)


def _parity_value(value: Any) -> str:
    if value is None:
        return "<none>"
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return _compact(value)
    return str(value)


def compare_offer_sets(
    legacy: Sequence[RawOffer],
    generic: Sequence[RawOffer],
) -> SourceParityReport:
    legacy_by_id = {offer.external_id: offer for offer in legacy}
    generic_by_id = {offer.external_id: offer for offer in generic}
    missing = tuple(sorted(set(legacy_by_id) - set(generic_by_id)))
    extra = tuple(sorted(set(generic_by_id) - set(legacy_by_id)))
    shared = tuple(sorted(set(legacy_by_id) & set(generic_by_id)))
    mismatches: list[ParityMismatch] = []

    for external_id_value in shared:
        expected = legacy_by_id[external_id_value]
        candidate = generic_by_id[external_id_value]
        for field_name in _PARITY_FIELDS:
            legacy_value = getattr(expected, field_name)
            if legacy_value is None:
                continue
            generic_value = getattr(candidate, field_name)
            if _parity_value(legacy_value) != _parity_value(generic_value):
                mismatches.append(
                    ParityMismatch(
                        external_id=external_id_value,
                        field=field_name,
                        legacy_value=_parity_value(legacy_value),
                        generic_value=_parity_value(generic_value),
                    )
                )

    safe = (
        not missing
        and not extra
        and not mismatches
        and len(legacy_by_id) == len(legacy)
        and len(generic_by_id) == len(generic)
    )
    return SourceParityReport(
        safe_to_adopt=safe,
        legacy_count=len(legacy),
        generic_count=len(generic),
        matched_count=len(shared),
        missing_ids=missing,
        extra_ids=extra,
        mismatches=tuple(mismatches),
    )
