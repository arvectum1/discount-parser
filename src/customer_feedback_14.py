from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html import escape as html_escape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from sqlalchemy import select

from src.core import conditions as _conditions
from src.modules.offers import repository as _offer_repository
from src.modules.source_registry import assisted_setup as _assisted
from src.modules.source_registry import collectors as _collectors
from src.modules.source_registry import runner as _registry_runner
from src.modules.source_registry import service as _registry_service
from src.sources.adapters import promokood as _promokood
from src.sources import runner as _sources_runner
from src.telegram import render as _telegram_render


_PATCHED = False

_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_EXPLICIT_PROMO_RE = re.compile(
    r"(?<![\w/])(?:промокод(?:у|а|ом|е|ы|ов)?|promo\s*code|код)\b"
    r"\s*[:\-–—]?\s*([A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9_-]{3,24})\b",
    re.IGNORECASE,
)
_STANDALONE_PROMO_RE = re.compile(r"^[A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9_-]{4,24}$")
_CTA_ONLY_RE = re.compile(
    r"^(?:активировать|получить|открыть|использовать|применить)"
    r"(?:\s+(?:промокод|скидку|предложение))?$",
    re.IGNORECASE,
)
_PROMO_STOPWORDS = {
    "АКТИВИРОВАТЬ", "ПРОМОКОД", "ПРОМОКОДЫ", "СКИДКА", "СКИДКИ",
    "ПОЛУЧИТЬ", "ОТКРЫТЬ", "ПРИМЕНИТЬ", "ИСПОЛЬЗОВАТЬ", "ПРЕДЛОЖЕНИЕ",
}
_ADVERTISER_RE = re.compile(
    r"(?:Рекламодатель|Продавец|Поставщик)\s*[:—-]?\s*(.+?)(?:[,;]\s*ИНН\b|\s+ИНН\b|$)",
    re.IGNORECASE,
)
_FALLBACK_CONDITION_RE = re.compile(
    r"(?:скидк\w*\s+\d{1,3}(?:[.,]\d+)?\s*%|кэшб\w*|кешб\w*|"
    r"бесплатн\w*\s+доставк\w*|промокод\w*)"
    r".{0,220}\b(?:на|при|для|от|до|кажд\w*|перв\w*|заказ\w*|покупк\w*|магазин\w*)\b",
    re.IGNORECASE,
)

_ORIGINAL_DETECT = _registry_service.detect_offer_signal
_ORIGINAL_CONDITIONS = _conditions.extract_conditions
_ORIGINAL_PROMOKOOD_PROPOSAL = _assisted._promokood_category_proposal
_ORIGINAL_DIRECT_PROPOSAL = _assisted._direct_known_site_proposal
_ORIGINAL_RENDER = _telegram_render.render_offer_caption


def _strip_urls(text: str) -> str:
    return _URL_RE.sub(" ", text or "")


def _clean_url(value: str) -> str:
    return value.rstrip(".,);]}>\"'")


def _extract_promo_code(text: str) -> str | None:
    clean = _strip_urls(text)
    explicit = _EXPLICIT_PROMO_RE.search(clean)
    if explicit:
        candidate = explicit.group(1).upper()
        if candidate not in _PROMO_STOPWORDS and not _CTA_ONLY_RE.fullmatch(candidate):
            return candidate

    for raw_line in clean.splitlines():
        line = raw_line.strip().strip("•—–-:;,.[](){}<>")
        if not line or line.upper() != line:
            continue
        if not _STANDALONE_PROMO_RE.fullmatch(line):
            continue
        candidate = line.upper()
        if candidate in _PROMO_STOPWORDS:
            continue
        if not any(ch.isdigit() for ch in candidate) and "_" not in candidate:
            continue
        return candidate
    return None


def _detect_offer_signal_v14(text: str, keywords=()):
    clean = _strip_urls(text)
    base = _ORIGINAL_DETECT(clean, keywords)
    promo_code = _extract_promo_code(text)

    score = int(base.confidence)
    if promo_code and not base.promo_code:
        score += 4
    elif base.promo_code and not promo_code:
        score = max(0, score - 4)

    offer_type = "promo" if promo_code else base.offer_type
    return _registry_service.OfferSignal(
        is_offer=bool(base.is_offer or score >= 4),
        confidence=score,
        offer_type=offer_type,
        matched_keywords=base.matched_keywords,
        promo_code=promo_code,
        discount_percent=base.discount_percent,
        old_price=base.old_price,
        new_price=base.new_price,
    )


def _extract_conditions_v14(*parts: str | None, explicit: str | None = None):
    result = _ORIGINAL_CONDITIONS(*parts, explicit=explicit)
    if result.conditions or (explicit and explicit.strip()):
        return result

    text = "\n".join(part for part in parts if part)
    selected: list[str] = []
    for chunk in re.split(r"(?<=[.!?;])\s+|\n+", text):
        cleaned = " ".join(chunk.split()).strip(" •—–-\t")
        if cleaned and _FALLBACK_CONDITION_RE.search(cleaned):
            selected.append(cleaned)
    conditions = " ".join(dict.fromkeys(selected))[:2000] or None
    return _conditions.ConditionResult(
        conditions=conditions,
        max_discount_amount=result.max_discount_amount,
        min_order_amount=result.min_order_amount,
    )


def _nearest_offer_card(action: Tag) -> Tag:
    action_text = re.sub(r"\s+", " ", action.get_text(" ", strip=True)).strip()
    if not _CTA_ONLY_RE.fullmatch(action_text) and len(action_text) >= 20:
        return action

    for parent in action.parents:
        if not isinstance(parent, Tag):
            continue
        text = re.sub(r"\s+", " ", " ".join(parent.stripped_strings)).strip()
        if len(text) <= len(action_text) + 5:
            continue
        if len(text) > 1200:
            break
        if parent.name in {"article", "li", "section", "div"}:
            return parent
    return action


def _promokood_parse_v14(self, html: str):
    soup = BeautifulSoup(html, "html.parser")
    offers = []
    seen_cards: set[str] = set()

    for action in soup.find_all(["a", "button"]):
        action_text = re.sub(r"\s+", " ", action.get_text(" ", strip=True)).strip()
        if not action_text or not _promokood._OFFER_WORD_RE.search(action_text):
            continue

        card = _nearest_offer_card(action)
        card_text = re.sub(r"\s+", " ", " ".join(card.stripped_strings)).strip()
        if len(card_text) < 8:
            continue

        signature = re.sub(r"\s+", " ", card_text.casefold())
        if signature in seen_cards:
            continue
        seen_cards.add(signature)

        href = action.get("href") if isinstance(action, Tag) else None
        if href and str(href).lower().startswith(("javascript:", "#")):
            href = None
        source_url = urljoin(self.base_url, href) if href else self.base_url

        merchant = self._merchant(card, action_text)
        title = self._title(card_text, merchant)
        title = re.sub(
            r"\s+(?:активировать|получить|открыть|использовать|применить)"
            r"(?:\s+(?:промокод|скидку|предложение))?$",
            "",
            title,
            flags=re.IGNORECASE,
        ).strip(" :-—")

        if _CTA_ONLY_RE.fullmatch(title):
            continue
        if len(title) > 260:
            title = title[:260].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "…"

        promo_code = _extract_promo_code(card_text)
        discount_percent = self._discount_percent(card_text)
        discount_amount = self._discount_amount(card_text) if discount_percent is None else None
        image_url = self._image_url(card)
        external_id = _promokood.hashlib.sha256(
            f"{self.base_url}|{signature}|{promo_code or ''}".encode("utf-8")
        ).hexdigest()[:32]

        offers.append(
            _promokood.RawOffer(
                source_key=self.key,
                external_id=external_id,
                title=title or merchant or "Предложение",
                source_url=source_url,
                merchant=merchant,
                description=card_text[:2000],
                conditions=_extract_conditions_v14(card_text).conditions,
                promo_code=promo_code,
                discount_percent=discount_percent,
                discount_amount=discount_amount,
                image_url=image_url,
                valid_until=_promokood.extract_valid_until(card_text),
                raw_payload={
                    "text": card_text,
                    "promo_code": promo_code,
                    "parser_version": "dp-cust-014",
                },
            )
        )
    return offers


def _preview_is_coherent(item) -> bool:
    title = " ".join(str(item.title or "").split()).strip()
    promo = " ".join(str(item.promo_code or "").split()).strip().upper()
    excerpt = " ".join(str(item.excerpt or "").split()).strip()

    if not title or _CTA_ONLY_RE.fullmatch(title):
        return False
    if title.casefold().startswith("активировать промокод"):
        return False
    if len(title) > 240:
        return False
    if promo:
        if promo in _PROMO_STOPWORDS or _CTA_ONLY_RE.fullmatch(promo):
            return False
        if not _STANDALONE_PROMO_RE.fullmatch(promo):
            return False
    if excerpt:
        promo_mentions = len(re.findall(r"\bпромокод\w*\b", excerpt, re.IGNORECASE))
        dates = len(re.findall(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b", excerpt))
        if promo_mentions >= 3 or dates >= 3:
            return False
    return True


def _guard_proposal(proposal):
    if proposal is None or not proposal.previews:
        return proposal
    coherent = all(_preview_is_coherent(item) for item in proposal.previews)
    if coherent:
        return proposal
    return replace(
        proposal,
        confidence=min(float(proposal.confidence), 0.55),
        explanation=(
            "Автоматический анализ нашёл страницу, но примеры распознаны неоднозначно. "
            "Настройка не будет применена автоматически; источник нужно перепроверить разработчику."
        ),
    )


def _promokood_category_proposal_v14(url, html_text, collector):
    return _guard_proposal(_ORIGINAL_PROMOKOOD_PROPOSAL(url, html_text, collector))


def _direct_known_site_proposal_v14(url, collector):
    return _guard_proposal(_ORIGINAL_DIRECT_PROPOSAL(url, collector))


def _decimal_metadata(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _structured_metadata(raw, adapter: str) -> dict:
    metadata = dict(raw.raw_payload or {})
    metadata.update(
        {
            "collector": "known_site_adapter",
            "adapter": adapter,
            "structured_fields": True,
            "merchant": raw.merchant,
            "brand": raw.brand,
            "promo_code": raw.promo_code,
            "conditions": raw.conditions,
            "discount_percent": str(raw.discount_percent) if raw.discount_percent is not None else None,
            "discount_amount": str(raw.discount_amount) if raw.discount_amount is not None else None,
            "old_price": str(raw.old_price) if raw.old_price is not None else None,
            "new_price": str(raw.new_price) if raw.new_price is not None else None,
            "cashback_percent": str(raw.cashback_percent) if raw.cashback_percent is not None else None,
            "cashback_amount": str(raw.cashback_amount) if raw.cashback_amount is not None else None,
            "delivery_price": str(raw.delivery_price) if raw.delivery_price is not None else None,
            "valid_until": raw.valid_until.isoformat() if raw.valid_until else None,
            "source_url": raw.source_url,
        }
    )
    return metadata


def _raw_offer_payload_v14(raw, *, adapter: str):
    return _registry_service.ItemPayload(
        external_id=raw.external_id,
        url=raw.source_url,
        title=raw.title,
        text=raw.description or raw.title,
        image_url=raw.image_url,
        raw_payload=_structured_metadata(raw, adapter),
    )


def _first_external_offer_url(text_node: Tag | None, text: str) -> str | None:
    candidates: list[str] = []
    if text_node is not None:
        for link in text_node.find_all("a", href=True):
            candidates.append(str(link.get("href") or ""))
    candidates.extend(_URL_RE.findall(text or ""))

    for candidate in candidates:
        url = _clean_url(candidate.strip())
        if not url.startswith(("http://", "https://")):
            continue
        host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
        if host in {"t.me", "telegram.me"}:
            continue
        return url
    return None


def _advertiser_from_text(text: str) -> str | None:
    match = _ADVERTISER_RE.search(text or "")
    if not match:
        return None
    value = " ".join(match.group(1).split()).strip(" ,.;:-")
    return value[:255] or None


def _telegram_collect_v14(self, source):
    channel = _collectors.normalize_telegram_channel(source.external_id or source.url)
    url = f"https://t.me/s/{channel}"
    retry = {403, 451} if source.network_policy == "auto" else set()
    response = self._get(url, route=source.network_policy, retry_statuses=retry)
    soup = BeautifulSoup(response.text, "html.parser")
    messages = soup.select(".tgme_widget_message[data-post]")
    if not soup.select_one(".tgme_channel_history") and not messages:
        raise _collectors.CollectorError(
            f"Telegram public preview returned no channel history (route={source.network_policy}, "
            f"status={response.status_code}, bytes={len(response.content)})"
        )

    result = []
    for message in messages:
        post_id = message.get("data-post")
        if not post_id or "/" not in post_id:
            continue
        post_channel, message_id = post_id.rsplit("/", 1)
        if not message_id.isdigit():
            continue

        wrapper = message.find_parent(class_="tgme_widget_message_wrap") or message.parent
        text_node = wrapper.select_one(".tgme_widget_message_text") if wrapper else None
        text = text_node.get_text("\n", strip=True) if text_node else ""
        if not text:
            continue

        source_post_url = f"https://t.me/{post_channel}/{message_id}"
        offer_url = _first_external_offer_url(text_node, text)
        time_node = wrapper.select_one("time")
        published_at = None
        if time_node and time_node.get("datetime"):
            try:
                published_at = datetime.fromisoformat(
                    str(time_node["datetime"]).replace("Z", "+00:00")
                )
            except ValueError:
                published_at = None

        image_node = wrapper.select_one("a.tgme_widget_message_photo_wrap")
        image_url = None
        if image_node:
            style = image_node.get("style", "")
            match = re.search(r"background-image:url\(['\"]?([^'\")]+)", style)
            if match:
                image_url = match.group(1)

        merchant = _advertiser_from_text(text)
        result.append(
            _registry_service.ItemPayload(
                external_id=f"telegram:{post_channel}:{message_id}",
                url=offer_url or source_post_url,
                title=None,
                text=text[:12000],
                published_at=published_at,
                author=post_channel,
                image_url=image_url,
                raw_payload={
                    "collector": "telegram_public",
                    "network_policy": source.network_policy,
                    "telegram_post_id": post_id,
                    "telegram_channel": post_channel,
                    "source_post_url": source_post_url,
                    "offer_url": offer_url,
                    "merchant": merchant,
                    "promo_code": _extract_promo_code(text),
                },
            )
        )
        if len(result) >= self.policy.max_items:
            break
    return result


def _metadata_datetime(value, fallback_text: str):
    if isinstance(value, datetime):
        return value
    if value:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed
        except ValueError:
            parsed = _registry_runner.extract_valid_until(text)
            if parsed is not None:
                return parsed
    return _registry_runner.extract_valid_until(fallback_text)


def _text_value(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def _structured_benefit(metadata: dict) -> bool:
    return any(
        metadata.get(key) not in (None, "")
        for key in (
            "promo_code",
            "discount_percent",
            "discount_amount",
            "cashback_percent",
            "cashback_amount",
            "delivery_price",
        )
    )


def _collect_registered_source_v14(source_id: int):
    started = datetime.now(UTC)
    with _registry_runner.session_scope() as session:
        source = session.get(_registry_runner.RegisteredSource, source_id)
        if source is None:
            raise KeyError(source_id)
        result = _registry_runner.RegistryRunResult(source_key=source.key)
        if not source.enabled:
            source.status = "disabled"
            return result
        if source.collector_type == "legacy_adapter":
            return result
        collector_type = source.collector_type
        source_url = source.url

    try:
        collector = _registry_runner.build_collector(collector_type)
        with _registry_runner.session_scope() as session:
            source = session.get(_registry_runner.RegisteredSource, source_id)
            assert source is not None
            payloads = collector.collect(source)
    except _registry_runner.CredentialsRequired as exc:
        with _registry_runner.session_scope() as session:
            source = session.get(_registry_runner.RegisteredSource, source_id)
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
        with _registry_runner.session_scope() as session:
            source = session.get(_registry_runner.RegisteredSource, source_id)
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
    with _registry_runner.session_scope() as session:
        source = session.get(_registry_runner.RegisteredSource, source_id)
        assert source is not None
        legacy_source = _registry_runner._legacy_source(session, source)
        keywords = _registry_runner._keywords_for_source(session, source)

        for payload in payloads:
            item = None
            try:
                item, created = _registry_runner.upsert_source_item(session, source, payload)
                result.items_created += int(created)
                metadata = dict(payload.raw_payload or {})
                combined_text = "\n".join(part for part in (payload.title, payload.text) if part)

                valid_until = _metadata_datetime(metadata.get("valid_until"), combined_text)
                if valid_until is not None:
                    comparable = valid_until if valid_until.tzinfo is not None else valid_until.replace(tzinfo=UTC)
                    if comparable < datetime.now(UTC):
                        item.processing_status = "ignored"
                        item.raw_payload_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
                        result.ignored += 1
                        continue

                signal = _registry_runner.detect_offer_signal(combined_text, keywords)
                if not signal.is_offer and not _structured_benefit(metadata):
                    item.processing_status = "ignored"
                    item.raw_payload_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
                    result.ignored += 1
                    continue
                result.offer_signals += 1

                promo_code = (
                    _registry_runner._resolve_promko_code(
                        session,
                        source=source,
                        legacy_source=legacy_source,
                        payload=payload,
                        item_created=created,
                        metadata=metadata,
                        result=result,
                    )
                    or _text_value(metadata.get("promo_code"))
                    or signal.promo_code
                )
                if promo_code:
                    promo_code = promo_code.upper()

                conditions = _text_value(metadata.get("conditions"))
                if not conditions:
                    conditions = _extract_conditions_v14(
                        payload.title,
                        payload.text,
                    ).conditions

                merchant = _text_value(metadata.get("merchant")) or source.merchant
                brand = _text_value(metadata.get("brand")) or source.brand
                title = (
                    _text_value(metadata.get("title"))
                    or _text_value(payload.title)
                    or _text_value(payload.text)
                    or "Предложение"
                )
                title = title.splitlines()[0][:1000]

                item.raw_payload_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
                raw = _registry_runner.RawOffer(
                    source_key=legacy_source.key,
                    external_id=payload.external_id or item.content_hash,
                    title=title,
                    source_url=_text_value(metadata.get("offer_url")) or payload.url or source_url,
                    merchant=merchant,
                    brand=brand,
                    description=payload.text,
                    conditions=conditions,
                    promo_code=promo_code,
                    discount_percent=(
                        _decimal_metadata(metadata.get("discount_percent"))
                        if metadata.get("discount_percent") not in (None, "")
                        else signal.discount_percent
                    ),
                    discount_amount=_decimal_metadata(metadata.get("discount_amount")),
                    old_price=(
                        _decimal_metadata(metadata.get("old_price"))
                        if metadata.get("old_price") not in (None, "")
                        else signal.old_price
                    ),
                    new_price=(
                        _decimal_metadata(metadata.get("new_price"))
                        if metadata.get("new_price") not in (None, "")
                        else signal.new_price
                    ),
                    cashback_percent=_decimal_metadata(metadata.get("cashback_percent")),
                    cashback_amount=_decimal_metadata(metadata.get("cashback_amount")),
                    delivery_price=_decimal_metadata(metadata.get("delivery_price")),
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
                        "field_precedence": "structured_then_heuristic",
                    },
                )
                outcome = _registry_runner._persist_raw_offer(session, legacy_source, raw)
                if outcome == "created":
                    result.offers_created += 1
                elif outcome == "updated":
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


def _set_manual_override_v14(self, offer, field_name: str, value, source: str = "manual"):
    if not hasattr(offer, field_name):
        raise ValueError(f"unknown Offer field: {field_name}")
    override = self.session.scalar(
        select(_offer_repository.ManualOverride).where(
            _offer_repository.ManualOverride.offer_id == offer.id,
            _offer_repository.ManualOverride.field_name == field_name,
        )
    )
    stored_value = None
    if value is not None:
        stored_value = value.isoformat() if hasattr(value, "isoformat") else str(value)
    if override is None:
        override = _offer_repository.ManualOverride(
            offer_id=offer.id,
            field_name=field_name,
            value=stored_value,
            source=source,
        )
        self.session.add(override)
    else:
        override.value = stored_value
        override.source = source
    setattr(offer, field_name, value)
    self.session.flush()
    return override


def _render_offer_caption_v14(offer, publication_format=None) -> str:
    caption = _ORIGINAL_RENDER(offer, publication_format)
    url = str(getattr(offer, "canonical_url", "") or "").strip()
    if not url.startswith(("http://", "https://")):
        return caption
    safe = html_escape(url, quote=True)
    return caption + f'\n🔗 <a href="{safe}">Ссылка на предложение</a>'


def _install_xlsx_patch() -> None:
    try:
        from src.modules.xlsx import service as xlsx
    except Exception:
        return

    if "display_title" not in xlsx.OFFER_HEADERS:
        headers = list(xlsx.OFFER_HEADERS)
        headers.insert(headers.index("title") + 1, "display_title")
        xlsx.OFFER_HEADERS = headers

    editable = {
        "display_title",
        "merchant",
        "category",
        "subcategory",
        "geo_scope",
        "region",
        "city",
        "conditions",
        "max_discount_amount",
        "min_order_amount",
        "discount_percent",
        "discount_amount",
        "promo_code",
        "old_price",
        "new_price",
        "cashback_percent",
        "cashback_amount",
        "delivery_price",
        "valid_until",
        "canonical_url",
    }
    xlsx.EDITABLE_COLUMNS = editable

    decimal_fields = {
        "max_discount_amount",
        "min_order_amount",
        "discount_percent",
        "discount_amount",
        "old_price",
        "new_price",
        "cashback_percent",
        "cashback_amount",
        "delivery_price",
    }

    def coerce(field_name: str, value):
        if value is None or value == "":
            return None
        if field_name in decimal_fields:
            if isinstance(value, Decimal):
                return value
            return Decimal(str(value).replace(" ", "").replace(",", "."))
        if field_name == "valid_until":
            if isinstance(value, datetime):
                return value
            text = str(value).strip()
            for parser in (
                lambda: datetime.fromisoformat(text.replace("Z", "+00:00")),
                lambda: datetime.strptime(text, "%d.%m.%Y").replace(tzinfo=UTC),
            ):
                try:
                    return parser()
                except ValueError:
                    continue
            raise ValueError("invalid date")
        if field_name == "geo_scope":
            value = str(value).strip()
            if value not in {"unknown", "all_russia", "region", "city"}:
                raise ValueError("invalid geo_scope")
            return value
        if field_name == "canonical_url":
            value = str(value).strip()
            if value and not value.startswith(("http://", "https://")):
                raise ValueError("URL must start with http:// or https://")
            return value or None
        return str(value).strip() or None

    def same_value(left, right) -> bool:
        if isinstance(left, Decimal) or isinstance(right, Decimal):
            try:
                return Decimal(str(left)) == Decimal(str(right))
            except Exception:
                return False
        if isinstance(left, datetime) and isinstance(right, datetime):
            a = left if left.tzinfo else left.replace(tzinfo=UTC)
            b = right if right.tzinfo else right.replace(tzinfo=UTC)
            return a == b
        return left == right

    def import_offer_corrections_v14(path, *, create_rules: bool = True):
        report = xlsx.ImportReport()
        workbook = xlsx.CalamineWorkbook.from_path(str(path))
        with xlsx.create_session() as session:
            repo = xlsx.OfferRepository(session)
            for sheet_name in xlsx.OFFER_SHEETS:
                if sheet_name not in workbook.sheet_names:
                    continue
                rows = workbook.get_sheet_by_name(sheet_name).to_python()
                if not rows:
                    continue
                headers = [str(value).strip() if value is not None else "" for value in rows[0]]
                index = {header: position for position, header in enumerate(headers) if header}
                if "id" not in index:
                    report.errors.append(f"{sheet_name}: missing id column")
                    continue

                fields = [field for field in xlsx.EDITABLE_COLUMNS if field in index]
                if not fields:
                    report.errors.append(f"{sheet_name}: no editable columns")
                    continue

                for row_number, row in enumerate(rows[1:], start=2):
                    if not row or index["id"] >= len(row) or row[index["id"]] in (None, ""):
                        continue
                    report.rows_seen += 1
                    try:
                        offer_id = int(float(row[index["id"]]))
                    except (TypeError, ValueError):
                        report.rows_skipped += 1
                        report.errors.append(f"{sheet_name}!{row_number}: invalid id")
                        continue

                    offer = session.get(xlsx.Offer, offer_id)
                    if offer is None:
                        report.rows_skipped += 1
                        report.errors.append(f"{sheet_name}!{row_number}: offer {offer_id} not found")
                        continue

                    changed = False
                    category_changed = False
                    for field_name in fields:
                        position = index[field_name]
                        raw_value = row[position] if position < len(row) else None
                        try:
                            value = coerce(field_name, raw_value)
                        except (ValueError, InvalidOperation) as exc:
                            report.errors.append(
                                f"{sheet_name}!{row_number}:{field_name}: {exc}"
                            )
                            continue
                        if same_value(value, getattr(offer, field_name)):
                            continue
                        repo.set_manual_override(offer, field_name, value, source="xlsx")
                        report.overrides_written += 1
                        changed = True
                        category_changed = category_changed or field_name in {"category", "subcategory"}

                    if not changed:
                        report.rows_skipped += 1
                        continue

                    report.rows_changed += 1
                    if (
                        offer.status == "needs_review"
                        and offer.category
                        and offer.category != "Другое"
                        and xlsx._offer_has_benefit(offer)
                    ):
                        offer.status = "ready"
                    if create_rules and category_changed and xlsx._create_conservative_rule(session, offer):
                        report.rules_created += 1
            session.commit()
        return report

    xlsx.import_offer_corrections = import_offer_corrections_v14


def install_customer_feedback_14() -> None:
    global _PATCHED
    if _PATCHED:
        return

    _registry_service.detect_offer_signal = _detect_offer_signal_v14
    _conditions.extract_conditions = _extract_conditions_v14
    _promokood.PromokoodAdapter.parse = _promokood_parse_v14
    _assisted._promokood_category_proposal = _promokood_category_proposal_v14
    _assisted._direct_known_site_proposal = _direct_known_site_proposal_v14
    _collectors.GenericWebCollector._raw_offer_payload = staticmethod(_raw_offer_payload_v14)
    _collectors.TelegramPublicCollector.collect = _telegram_collect_v14
    _registry_runner.detect_offer_signal = _detect_offer_signal_v14
    _sources_runner.extract_conditions = _extract_conditions_v14
    _registry_runner.collect_registered_source = _collect_registered_source_v14
    _offer_repository.OfferRepository.set_manual_override = _set_manual_override_v14
    _telegram_render.render_offer_caption = _render_offer_caption_v14

    _install_xlsx_patch()
    _PATCHED = True
