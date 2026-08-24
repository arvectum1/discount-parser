from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from src.core import conditions as condition_service
from src.core.validity import extract_valid_until
from src.modules.source_registry.service import ItemPayload
from src.sources.base import RawOffer


STRUCTURING_VERSION = "dp-cust-015"
AUTO_READY_THRESHOLD = 0.80

_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_CTA_ONLY_RE = re.compile(
    r"^(?:активировать|получить|открыть|использовать|применить|перейти|забрать)"
    r"(?:\s+(?:промокод|скидку|предложение|акцию))?$",
    re.IGNORECASE,
)
_PROMO_LABEL_RE = re.compile(
    r"(?<![\w/])(?:промокод(?:у|а|ом|е|ы|ов)?|promo\s*code|coupon\s*code)\b"
    r"\s*[:\-–—]?\s*([A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9_-]{3,31})\b",
    re.IGNORECASE,
)
_CODE_WITH_SEPARATOR_RE = re.compile(
    r"(?<![\w/])код\b\s*[:\-–—]\s*([A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9_-]{3,31})\b",
    re.IGNORECASE,
)
_STANDALONE_CODE_RE = re.compile(r"^[A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9_-]{4,31}$")
_PERCENT_RE = re.compile(r"(?<!\d)(\d{1,3}(?:[.,]\d+)?)\s*%", re.IGNORECASE)
_CASHBACK_PERCENT_RE = re.compile(
    r"(?:кэшб\w*|кешб\w*|cashback)\D{0,30}(\d{1,3}(?:[.,]\d+)?)\s*%",
    re.IGNORECASE,
)
_DISCOUNT_AMOUNT_RE = re.compile(
    r"скидк\w*\D{0,18}(\d{1,3}(?:[\s\u00a0]\d{3})*|\d{2,7})(?:[.,]\d{1,2})?\s*(?:₽|руб(?:\.|лей|ля)?)",
    re.IGNORECASE,
)
_CASHBACK_AMOUNT_RE = re.compile(
    r"(?:кэшб\w*|кешб\w*|cashback)\D{0,18}(\d{1,3}(?:[\s\u00a0]\d{3})*|\d{2,7})(?:[.,]\d{1,2})?\s*(?:₽|руб(?:\.|лей|ля)?)",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:[\s\u00a0]\d{3})+|\d{2,7})(?:[.,](\d{1,2}))?\s*(?:₽|руб(?:\.|лей|ля)?)",
    re.IGNORECASE,
)
_ADVERTISER_RE = re.compile(
    r"(?:Рекламодатель|Продавец|Поставщик|Магазин)\s*[:—-]?\s*(.+?)"
    r"(?:[,;]\s*ИНН\b|\s+ИНН\b|\s+erid\b|$)",
    re.IGNORECASE,
)
_OFFER_WORD_RE = re.compile(
    r"\b(?:скидк\w*|промокод\w*|акци\w*|распродаж\w*|кэшб\w*|кешб\w*|"
    r"cashback|sale|спецпредлож\w*|бесплатн\w*\s+доставк\w*|\d+\s+по\s+цене\s+\d+)\b",
    re.IGNORECASE,
)
_NOISE_LINE_RE = re.compile(
    r"^(?:реклама\.?|erid\b.*|инн\b.*|рекламодатель\b.*|подробнее|читать далее)$",
    re.IGNORECASE,
)
_GENERIC_TITLES = {"предложение", "акция", "скидка", "промокод", "спецпредложение"}
_PROMO_STOPWORDS = {
    "АКТИВИРОВАТЬ", "ПРОМОКОД", "ПРОМОКОДЫ", "СКИДКА", "СКИДКИ", "ПОЛУЧИТЬ",
    "ОТКРЫТЬ", "ПРИМЕНИТЬ", "ИСПОЛЬЗОВАТЬ", "ПРЕДЛОЖЕНИЕ", "ПЕРЕЙТИ", "ЗАБРАТЬ",
    "PROMOCODE", "PROMO", "SALE", "COUPON", "ТОВАРА", "ТОВАР", "ЗАКАЗА", "ЗАКАЗ",
    "АКЦИИ", "МАРКЕТЕ", "МАГАЗИНЕ", "ДОСТАВКА", "БЕСПЛАТНО",
}
_SOCIAL_HOSTS = {
    "t.me", "telegram.me", "vk.com", "dzen.ru", "zen.yandex.ru", "rutube.ru",
}


@dataclass(frozen=True, slots=True)
class StructuredOffer:
    raw: RawOffer
    confidence: float
    issues: tuple[str, ...]
    auto_ready: bool
    accepted: bool


@dataclass(frozen=True, slots=True)
class StructuringBatch:
    candidates: tuple[StructuredOffer, ...]
    disposition: str  # processed | needs_review | ignored
    reason: str | None = None


def _text(value: Any) -> str | None:
    cleaned = " ".join(str(value or "").split()).strip()
    return cleaned or None


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _strip_urls_preserving_length(text: str) -> str:
    return _URL_RE.sub(lambda match: " " * len(match.group(0)), text or "")


def _clean_url(value: Any) -> str | None:
    text = str(value or "").strip().rstrip(".,);]}>\"'")
    if not text.startswith(("http://", "https://")):
        return None
    parsed = urlparse(text)
    if not parsed.hostname:
        return None
    return text


def _external_offer_url(text: str, fallback: str | None) -> str | None:
    fallback_url = _clean_url(fallback)
    fallback_host = (urlparse(fallback_url).hostname or "").casefold().removeprefix("www.") if fallback_url else ""
    for match in _URL_RE.finditer(text or ""):
        candidate = _clean_url(match.group(0))
        if not candidate:
            continue
        host = (urlparse(candidate).hostname or "").casefold().removeprefix("www.")
        if host in _SOCIAL_HOSTS:
            continue
        if fallback_host in _SOCIAL_HOSTS or not fallback_url:
            return candidate
    return fallback_url


def _valid_promo(value: str | None) -> bool:
    candidate = str(value or "").strip().upper()
    if not candidate or candidate in _PROMO_STOPWORDS:
        return False
    if _CTA_ONLY_RE.fullmatch(candidate):
        return False
    return bool(_STANDALONE_CODE_RE.fullmatch(candidate))


def _promo_candidates(text: str) -> list[str]:
    clean = _strip_urls_preserving_length(text)
    result: list[str] = []
    for regex in (_PROMO_LABEL_RE, _CODE_WITH_SEPARATOR_RE):
        for match in regex.finditer(clean):
            candidate = match.group(1).upper()
            if _valid_promo(candidate) and candidate not in result:
                result.append(candidate)

    for line in clean.splitlines():
        original = line.strip().strip("•—–-:;,.[](){}<>")
        candidate = original.upper()
        if not candidate or candidate in result or candidate != original:
            continue
        if _valid_promo(candidate) and (any(char.isdigit() for char in candidate) or "_" in candidate):
            result.append(candidate)
    return result


def _decimal_matches(regex: re.Pattern[str], text: str) -> list[Decimal]:
    result: list[Decimal] = []
    for match in regex.finditer(text):
        value = _decimal(match.group(1))
        if value is not None and value not in result:
            result.append(value)
    return result


def _price_values(text: str) -> list[Decimal]:
    values: list[Decimal] = []
    for match in _PRICE_RE.finditer(text):
        whole = match.group(1).replace("\u00a0", "").replace(" ", "")
        fraction = match.group(2)
        value = _decimal(f"{whole}.{fraction}" if fraction else whole)
        if value is not None:
            values.append(value)
    return values


def _merchant_from_text(text: str) -> str | None:
    match = _ADVERTISER_RE.search(text or "")
    if not match:
        return None
    value = " ".join(match.group(1).split()).strip(" ,.;:-")
    return value[:255] or None


def _meaningful_title(candidate: str | None) -> bool:
    text = _text(candidate)
    if not text:
        return False
    if text.casefold().strip(" .:-—") in _GENERIC_TITLES:
        return False
    if _CTA_ONLY_RE.fullmatch(text) or _NOISE_LINE_RE.fullmatch(text):
        return False
    if _valid_promo(text.upper()) and text.upper() == text:
        return False
    if _clean_url(text):
        return False
    return len(text) >= 4


def _title_from_text(text: str, merchant: str | None, benefit_label: str | None) -> tuple[str | None, bool]:
    lines = [" ".join(line.split()).strip(" •\t") for line in (text or "").splitlines()]
    lines = [
        line for line in lines
        if _meaningful_title(line) and not line.casefold().startswith("рекламодатель")
    ]
    preferred = next((line for line in lines if _OFFER_WORD_RE.search(line)), None)
    title = preferred or (lines[0] if lines else None)
    if not title:
        parts = [part for part in (benefit_label, merchant) if part]
        title = " — ".join(parts) if parts else None

    truncated = False
    if title and len(title) > 240:
        sentence = re.split(r"(?<=[.!?;])\s+", title, maxsplit=1)[0].strip()
        if 4 <= len(sentence) <= 240:
            title = sentence
        else:
            title = title[:240].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "…"
        truncated = True
    return title, truncated


def _benefit_label(
    promo_code: str | None,
    discount_percent: Decimal | None,
    discount_amount: Decimal | None,
    cashback_percent: Decimal | None,
    cashback_amount: Decimal | None,
    free_delivery: bool,
) -> str | None:
    if discount_percent is not None:
        return f"Скидка {discount_percent.normalize()}%"
    if discount_amount is not None:
        return f"Скидка {discount_amount.normalize()} ₽"
    if cashback_percent is not None:
        return f"Кэшбэк {cashback_percent.normalize()}%"
    if cashback_amount is not None:
        return f"Кэшбэк {cashback_amount.normalize()} ₽"
    if free_delivery:
        return "Бесплатная доставка"
    if promo_code:
        return f"Промокод {promo_code}"
    return None


def _structured_value(metadata: dict[str, Any], key: str, fallback: Any = None) -> Any:
    value = metadata.get(key)
    return fallback if value in (None, "") else value


def _datetime(value: Any, fallback_text: str) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value:
        text = str(value).strip()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = extract_valid_until(text)
            if parsed is not None:
                return parsed
    return extract_valid_until(fallback_text)


def _quality(
    *,
    title: str | None,
    merchant: str | None,
    conditions: str | None,
    source_url: str | None,
    structured_fields: bool,
    has_benefit: bool,
    issues: list[str],
) -> float:
    score = 0.15
    if _meaningful_title(title):
        score += 0.25
    if has_benefit:
        score += 0.25
    if merchant:
        score += 0.10
    if conditions:
        score += 0.10
    if source_url:
        score += 0.08
    if structured_fields:
        score += 0.12
    if any(issue.startswith("multiple_") for issue in issues):
        score -= 0.25
    if "title_truncated" in issues:
        score -= 0.05
    return max(0.0, min(1.0, round(score, 2)))


def structure_raw_offer(raw: RawOffer) -> StructuredOffer:
    metadata = dict(raw.raw_payload or {})
    if metadata.get("structuring_version") == STRUCTURING_VERSION:
        return StructuredOffer(
            raw=raw,
            confidence=float(metadata.get("structuring_confidence") or 0.0),
            issues=tuple(str(value) for value in (metadata.get("structuring_issues") or [])),
            auto_ready=bool(metadata.get("structuring_auto_ready", False)),
            accepted=bool(metadata.get("structuring_accepted", True)),
        )

    original_text = "\n".join(part for part in (raw.title, raw.description) if part)
    clean_text = _strip_urls_preserving_length(original_text)
    structured_fields = bool(metadata.get("structured_fields"))
    issues: list[str] = []

    promo_candidates = _promo_candidates(original_text)
    explicit_promo = _text(_structured_value(metadata, "promo_code", raw.promo_code))
    promo_code = explicit_promo.upper() if explicit_promo and _valid_promo(explicit_promo) else None
    if explicit_promo and promo_code is None:
        issues.append("invalid_structured_promo")
    if promo_code is None and promo_candidates:
        promo_code = promo_candidates[0]
    if len(promo_candidates) > 1 and not structured_fields:
        issues.append("multiple_promo_codes")

    percent_values = _decimal_matches(_PERCENT_RE, clean_text)
    cashback_values = _decimal_matches(_CASHBACK_PERCENT_RE, clean_text)
    discount_percent = _decimal(_structured_value(metadata, "discount_percent", raw.discount_percent))
    cashback_percent = _decimal(_structured_value(metadata, "cashback_percent", raw.cashback_percent))
    if cashback_percent is None and cashback_values:
        cashback_percent = cashback_values[0]
    if discount_percent is None:
        non_cashback = [value for value in percent_values if value not in cashback_values]
        if non_cashback:
            discount_percent = non_cashback[0]
    if len(set(percent_values)) > 2 and not structured_fields:
        issues.append("multiple_discount_percentages")

    discount_amount = _decimal(_structured_value(metadata, "discount_amount", raw.discount_amount))
    if discount_amount is None:
        values = _decimal_matches(_DISCOUNT_AMOUNT_RE, clean_text)
        discount_amount = values[0] if values else None
    cashback_amount = _decimal(_structured_value(metadata, "cashback_amount", raw.cashback_amount))
    if cashback_amount is None:
        values = _decimal_matches(_CASHBACK_AMOUNT_RE, clean_text)
        cashback_amount = values[0] if values else None

    old_price = _decimal(_structured_value(metadata, "old_price", raw.old_price))
    new_price = _decimal(_structured_value(metadata, "new_price", raw.new_price))
    if old_price is None and new_price is None:
        prices = _price_values(clean_text)
        if len(prices) == 2 and prices[0] > prices[1]:
            old_price, new_price = prices
        elif len(set(prices)) > 2 and not structured_fields:
            issues.append("multiple_prices")

    free_delivery = bool(re.search(r"бесплатн\w*\s+доставк\w*", clean_text, re.IGNORECASE))
    delivery_price = _decimal(_structured_value(metadata, "delivery_price", raw.delivery_price))
    if free_delivery and delivery_price is None:
        delivery_price = Decimal("0")

    merchant = _text(_structured_value(metadata, "merchant", raw.merchant)) or _merchant_from_text(original_text)
    brand = _text(_structured_value(metadata, "brand", raw.brand))

    explicit_conditions = _text(_structured_value(metadata, "conditions", raw.conditions))
    condition_result = condition_service.extract_conditions(raw.title, raw.description, explicit=explicit_conditions)
    conditions = explicit_conditions or condition_result.conditions
    max_discount_amount = _decimal(
        _structured_value(metadata, "max_discount_amount", raw.max_discount_amount)
    ) or condition_result.max_discount_amount
    min_order_amount = _decimal(
        _structured_value(metadata, "min_order_amount", raw.min_order_amount)
    ) or condition_result.min_order_amount

    explicit_url = _clean_url(metadata.get("offer_url")) or _clean_url(metadata.get("source_url"))
    source_url = explicit_url or _external_offer_url(raw.description or "", raw.source_url)
    valid_until = _datetime(_structured_value(metadata, "valid_until", raw.valid_until), original_text)

    has_benefit = any(
        value is not None
        for value in (
            promo_code,
            discount_percent,
            discount_amount,
            cashback_percent,
            cashback_amount,
            delivery_price,
            old_price,
            new_price,
        )
    )
    has_offer_marker = bool(_OFFER_WORD_RE.search(clean_text))
    benefit_label = _benefit_label(
        promo_code,
        discount_percent,
        discount_amount,
        cashback_percent,
        cashback_amount,
        free_delivery,
    )

    preferred_title = _text(metadata.get("title")) or _text(raw.title)
    if preferred_title and (not _meaningful_title(preferred_title) or len(preferred_title) > 240):
        preferred_title = None
    derived_title, title_truncated = _title_from_text(raw.description or raw.title, merchant, benefit_label)
    title = preferred_title or derived_title
    if title_truncated:
        issues.append("title_truncated")
    if not _meaningful_title(title):
        issues.append("invalid_title")

    promo_word_count = len(re.findall(r"\bпромокод\w*\b", clean_text, re.IGNORECASE))
    date_count = len(re.findall(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b", clean_text))
    if promo_word_count >= 3 and not structured_fields:
        issues.append("multiple_offer_markers")
    if date_count >= 3 and len(original_text) > 300 and not structured_fields:
        issues.append("multiple_validity_markers")
    if not has_benefit and has_offer_marker:
        issues.append("benefit_needs_review")

    severe = {"multiple_promo_codes", "multiple_offer_markers", "invalid_title"}
    accepted = bool((has_benefit or has_offer_marker) and not severe.intersection(issues))
    confidence = _quality(
        title=title,
        merchant=merchant,
        conditions=conditions,
        source_url=source_url,
        structured_fields=structured_fields,
        has_benefit=has_benefit,
        issues=issues,
    )
    unique_issues = tuple(dict.fromkeys(issues))
    auto_ready = bool(
        accepted
        and has_benefit
        and confidence >= AUTO_READY_THRESHOLD
        and not unique_issues
    )

    enriched_payload = {
        **metadata,
        "structuring_version": STRUCTURING_VERSION,
        "structuring_confidence": confidence,
        "structuring_issues": list(unique_issues),
        "structuring_auto_ready": auto_ready,
        "structuring_accepted": accepted,
        "field_precedence": "structured_then_universal_heuristic",
    }
    structured_raw = replace(
        raw,
        title=title or "Предложение требует проверки",
        source_url=source_url or raw.source_url,
        merchant=merchant,
        brand=brand,
        conditions=conditions,
        max_discount_amount=max_discount_amount,
        min_order_amount=min_order_amount,
        promo_code=promo_code,
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        old_price=old_price,
        new_price=new_price,
        cashback_percent=cashback_percent,
        cashback_amount=cashback_amount,
        delivery_price=delivery_price,
        valid_until=valid_until,
        raw_payload=enriched_payload,
    )
    return StructuredOffer(
        raw=structured_raw,
        confidence=confidence,
        issues=unique_issues,
        auto_ready=auto_ready,
        accepted=accepted,
    )


def structure_registry_payload(
    payload: ItemPayload,
    *,
    source_key: str,
    source_url: str,
    source_merchant: str | None = None,
    source_brand: str | None = None,
    platform: str | None = None,
    signal: Any = None,
) -> StructuringBatch:
    metadata = dict(payload.raw_payload or {})
    combined_text = "\n".join(part for part in (payload.title, payload.text) if part)
    promo_codes = _promo_candidates(combined_text)
    structured_fields = bool(metadata.get("structured_fields"))

    if signal is not None:
        metadata.setdefault("signal_is_offer", bool(getattr(signal, "is_offer", False)))
        metadata.setdefault("signal_confidence", int(getattr(signal, "confidence", 0) or 0))
        metadata.setdefault("matched_keywords", list(getattr(signal, "matched_keywords", ()) or ()))
        signal_promo = _text(getattr(signal, "promo_code", None))
        if metadata.get("promo_code") in (None, "") and signal_promo and signal_promo.upper() in promo_codes:
            metadata["promo_code"] = signal_promo
        for key in ("discount_percent", "old_price", "new_price"):
            if metadata.get(key) in (None, ""):
                value = getattr(signal, key, None)
                metadata[key] = str(value) if value is not None else None

    if metadata.get("merchant") in (None, "") and source_merchant:
        metadata["merchant"] = source_merchant
    if metadata.get("brand") in (None, "") and source_brand:
        metadata["brand"] = source_brand
    metadata.setdefault("platform", platform)

    if len(promo_codes) > 1 and not structured_fields:
        return StructuringBatch(
            candidates=(),
            disposition="needs_review",
            reason=(
                "Найдено несколько разных промокодов в одном исходном блоке. "
                "Discount Parser не будет склеивать несколько акций в одну запись."
            ),
        )

    fallback_url = _clean_url(payload.url) or _clean_url(source_url) or source_url
    raw = RawOffer(
        source_key=source_key,
        external_id=payload.external_id or "",
        title=_text(metadata.get("title")) or _text(payload.title) or "Предложение",
        source_url=(
            _clean_url(metadata.get("offer_url"))
            or _clean_url(metadata.get("source_url"))
            or _external_offer_url(payload.text or "", fallback_url)
            or source_url
        ),
        merchant=_text(metadata.get("merchant")) or source_merchant,
        brand=_text(metadata.get("brand")) or source_brand,
        description=payload.text,
        conditions=_text(metadata.get("conditions")),
        max_discount_amount=_decimal(metadata.get("max_discount_amount")),
        min_order_amount=_decimal(metadata.get("min_order_amount")),
        promo_code=_text(metadata.get("promo_code")),
        discount_percent=_decimal(metadata.get("discount_percent")),
        discount_amount=_decimal(metadata.get("discount_amount")),
        old_price=_decimal(metadata.get("old_price")),
        new_price=_decimal(metadata.get("new_price")),
        cashback_percent=_decimal(metadata.get("cashback_percent")),
        cashback_amount=_decimal(metadata.get("cashback_amount")),
        delivery_price=_decimal(metadata.get("delivery_price")),
        image_url=payload.image_url,
        valid_from=payload.published_at,
        valid_until=_datetime(metadata.get("valid_until"), combined_text),
        raw_payload=metadata,
    )
    structured = structure_raw_offer(raw)
    signal_offer = bool(getattr(signal, "is_offer", False)) if signal is not None else False

    if not structured.accepted:
        if signal_offer or _OFFER_WORD_RE.search(_strip_urls_preserving_length(combined_text)):
            return StructuringBatch(
                candidates=(),
                disposition="needs_review",
                reason=(
                    "Предложение похоже на акцию, но поля нельзя надёжно разделить автоматически. "
                    "Запись не будет отправлена в публикацию."
                ),
            )
        return StructuringBatch(candidates=(), disposition="ignored", reason=None)

    return StructuringBatch(
        candidates=(structured,),
        disposition="processed" if structured.auto_ready else "needs_review",
        reason=None if structured.auto_ready else "Предложение распознано, но требует проверки перед публикацией.",
    )


def is_expired(value: datetime | None, *, now: datetime | None = None) -> bool:
    if value is None:
        return False
    comparable = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return comparable < (now or datetime.now(UTC))
