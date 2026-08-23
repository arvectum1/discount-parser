from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from src.core.normalization import decimal_or_none
from src.core.validity import extract_valid_until
from src.modules.source_registry import runner as registry_runner
from src.modules.source_registry import service as registry_service
from src.modules.source_registry.assisted_setup import AssistedSourceProposal
from src.modules.source_registry import assisted_setup
from src.modules.source_registry.collectors import GenericWebCollector, TelegramPublicCollector
from src.modules.source_registry.service import ItemPayload
from src.shared.db import session_scope
from src.sources.base import RawOffer
from src.sources.adapters.promokood import PromokoodAdapter
from src.sources import runner as source_runner
from src.telegram import render as telegram_render

_PATCH_MARKER = "_dp_cust_014_structured_extraction"
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_CODE_LINE_RE = re.compile(r"^[A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9_-]{3,24}$")
_GENERIC_ACTIONS = {
    "активировать", "активировать промокод", "получить промокод", "применить промокод",
    "использовать промокод", "открыть", "перейти", "получить скидку",
}
_PROMO_WORD_RE = re.compile(r"промокод|купон|promo\s*code", re.IGNORECASE)
_BENEFIT_RE = re.compile(r"скидк|промокод|кэшб|кешб|бонус|бесплат|\d{1,3}\s*%|\d[\d\s]{0,8}\s*(?:₽|руб)", re.IGNORECASE)
_CONDITION_LINE_RE = re.compile(
    r"(?:на\s+(?:каждый|первый|повторный|следующий)\s+заказ|для\s+(?:новых\s+)?(?:пользовател|клиент|заказ)|"
    r"при\s+заказ|заказ\w*\s+от|в\s+магазине|по\s+ссылке|до\s+\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|"
    r"не\s+суммируется|только\s+при|кроме|исключая)",
    re.IGNORECASE,
)


def _standalone_code(text: str) -> str | None:
    """Find a code printed on its own line without treating URL fragments as codes."""
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("•—–-:;,.()[]{}<>🔥⚡🎁✅🟩👉 ")
        if not line or line != line.upper() or not _CODE_LINE_RE.fullmatch(line):
            continue
        if not re.search(r"[A-ZА-ЯЁ]", line) or not re.search(r"\d", line):
            continue
        if line.casefold() in _GENERIC_ACTIONS:
            continue
        return line
    return None


def _safe_detect_offer_signal(text: str, keywords=()):
    """Run the legacy signal detector on prose only, then recover standalone codes."""
    cleaned = _URL_RE.sub(" ", text)
    signal = _ORIGINAL_DETECT(cleaned, keywords)
    if signal.promo_code:
        return signal
    code = _standalone_code(text)
    if not code:
        return signal
    return replace(
        signal,
        is_offer=True,
        confidence=max(signal.confidence, 8),
        offer_type="promo",
        promo_code=code,
    )


def _structured_payload(raw: RawOffer, *, adapter: str) -> ItemPayload:
    """Keep adapter-resolved fields authoritative instead of re-guessing them from text."""
    metadata = dict(raw.raw_payload or {})
    for key in (
        "merchant", "brand", "conditions", "promo_code", "discount_percent", "discount_amount",
        "old_price", "new_price", "cashback_percent", "cashback_amount", "delivery_price",
        "max_discount_amount", "min_order_amount",
    ):
        value = getattr(raw, key, None)
        if value is not None and value != "":
            metadata[key] = str(value) if key not in {"merchant", "brand", "conditions", "promo_code"} else value
    metadata.update({
        "collector": "known_site_adapter",
        "adapter": adapter,
        "valid_until": raw.valid_until.isoformat() if raw.valid_until else None,
        "structured_fields": True,
    })
    return ItemPayload(
        raw.external_id,
        raw.source_url,
        raw.title,
        raw.description or raw.title,
        image_url=raw.image_url,
        raw_payload=metadata,
    )


def _find_offer_card(action: Tag) -> Tag:
    action_text = " ".join(action.stripped_strings).strip()
    for parent in action.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name in {"article", "li"}:
            return parent
        if parent.name != "div":
            continue
        text = re.sub(r"\s+", " ", " ".join(parent.stripped_strings)).strip()
        if len(text) < len(action_text) + 8 or len(text) > 900:
            continue
        residual = text.replace(action_text, " ", 1).strip()
        if residual and _BENEFIT_RE.search(residual):
            # The first matching ancestor is the smallest coherent offer card.
            return parent
    return action


def _card_title(card: Tag, merchant: str | None, action_text: str) -> str:
    for selector in ("h2", "h3", "h4", "strong", "b"):
        for node in card.find_all(selector):
            value = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            if not value or value.casefold() in _GENERIC_ACTIONS or value == action_text:
                continue
            if merchant and value.casefold() == merchant.casefold():
                continue
            if 5 <= len(value) <= 220:
                return value
    for raw in card.stripped_strings:
        value = re.sub(r"\s+", " ", str(raw)).strip()
        if not value or value == action_text or value.casefold() in _GENERIC_ACTIONS:
            continue
        if merchant and value.casefold() == merchant.casefold():
            continue
        if _CODE_LINE_RE.fullmatch(value) or value.startswith(("http://", "https://")):
            continue
        if 8 <= len(value) <= 220 and _BENEFIT_RE.search(value):
            return value
    text = re.sub(r"\s+", " ", " ".join(card.stripped_strings)).strip()
    return text[:220] or merchant or "Предложение"


def _promokood_parse(self: PromokoodAdapter, html_text: str) -> list[RawOffer]:
    soup = BeautifulSoup(html_text, "html.parser")
    offers: list[RawOffer] = []
    seen_cards: set[str] = set()
    for action in soup.find_all(["a", "button"]):
        action_text = re.sub(r"\s+", " ", action.get_text(" ", strip=True)).strip()
        if not action_text or not _BENEFIT_RE.search(action_text):
            continue
        card = _find_offer_card(action)
        card_text = re.sub(r"\s+", " ", " ".join(card.stripped_strings)).strip()
        if len(card_text) < 12:
            continue
        card_key = hashlib.sha256(card_text.casefold().encode("utf-8")).hexdigest()
        if card_key in seen_cards:
            continue
        seen_cards.add(card_key)

        merchant = self._merchant(card, action_text)
        title = _card_title(card, merchant, action_text)
        labeled = re.search(
            r"(?:промокод(?:у|а|ом|е|ы|ов)?|promo\s*code|код)\b\s*[:\-–—]?\s*([A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9_-]{3,24})",
            card_text,
            re.IGNORECASE,
        )
        promo_code = labeled.group(1).upper() if labeled else _standalone_code("\n".join(card.stripped_strings))
        if promo_code and promo_code.casefold() in _GENERIC_ACTIONS:
            promo_code = None

        href = action.get("href") if isinstance(action, Tag) else None
        if not href:
            candidate_link = card.find("a", href=True)
            href = candidate_link.get("href") if candidate_link else None
        source_url = urljoin(self.base_url, href) if href else self.base_url
        discount_percent = self._discount_percent(card_text)
        discount_amount = self._discount_amount(card_text) if discount_percent is None else None
        external_id = hashlib.sha256(
            f"{self.base_url}|{merchant or ''}|{title}|{promo_code or ''}|{card_key}".encode("utf-8")
        ).hexdigest()[:32]
        offers.append(RawOffer(
            source_key=self.key,
            external_id=external_id,
            title=title,
            source_url=source_url,
            merchant=merchant,
            description=card_text[:2000],
            conditions=card_text[:2000],
            promo_code=promo_code,
            discount_percent=discount_percent,
            discount_amount=discount_amount,
            image_url=self._image_url(card),
            valid_until=extract_valid_until(card_text),
            raw_payload={"text": card_text, "promo_code": promo_code, "structured_fields": True},
        ))
    return offers


def _telegram_collect(self: TelegramPublicCollector, source) -> list[ItemPayload]:
    payloads = _ORIGINAL_TELEGRAM_COLLECT(self, source)
    result: list[ItemPayload] = []
    for payload in payloads:
        metadata = dict(payload.raw_payload or {})
        text = payload.text or ""
        outbound = None
        for match in _URL_RE.finditer(text):
            candidate = match.group(0).rstrip(".,;!?)\"]}")
            host = (urlparse(candidate).hostname or "").casefold().removeprefix("www.")
            if host and host not in {"t.me", "telegram.me"}:
                outbound = candidate
                break
        if outbound:
            metadata["offer_url"] = outbound
            metadata["source_post_url"] = payload.url
        result.append(replace(payload, raw_payload=metadata))
    return result


def _better_conditions(*parts: str | None, explicit: str | None = None):
    result = _ORIGINAL_CONDITIONS(*parts, explicit=explicit)
    if result.conditions:
        return result
    text = "\n".join(part for part in parts if part)
    candidates: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split()).strip(" •—–-\t")
        if not line or len(line) > 360 or line.startswith(("http://", "https://")):
            continue
        if "реклама" in line.casefold() or "erid=" in line.casefold():
            continue
        score = 0
        if _CONDITION_LINE_RE.search(line):
            score += 4
        if re.search(r"скидк|кэшб|кешб|бесплат", line, re.IGNORECASE):
            score += 2
        if re.search(r"\d{1,3}\s*%", line):
            score += 1
        if score >= 4:
            candidates.append((score, line))
    if not candidates:
        return result
    candidates.sort(key=lambda item: (-item[0], len(item[1])))
    return replace(result, conditions=candidates[0][1][:2000])


def _preview_is_coherent(item) -> bool:
    title = str(item.title or "").strip()
    promo = str(item.promo_code or "").strip()
    excerpt = str(item.excerpt or "").strip()
    if not title or len(title) > 220 or title.casefold() in _GENERIC_ACTIONS:
        return False
    if promo and (promo.casefold() in _GENERIC_ACTIONS or promo.casefold() in {"промокод", "promo"}):
        return False
    if title.casefold().count("промокод") > 2 or excerpt.casefold().count("промокод") > 4:
        return False
    return True


def _quality_guarded_promokood_proposal(url: str, html_text: str, collector: GenericWebCollector) -> AssistedSourceProposal | None:
    proposal = _ORIGINAL_PROMOKOOD_PROPOSAL(url, html_text, collector)
    if proposal is None:
        return None
    previews = list(proposal.previews)
    good = sum(_preview_is_coherent(item) for item in previews)
    if not previews or good < min(3, len(previews)):
        return replace(
            proposal,
            confidence=0.55,
            explanation=(
                "Каталог Promokood найден, но автоматический разбор примеров выглядит неоднозначно. "
                "Настройка не будет применена автоматически: источник нужно поправить в шаблоне парсера."
            ),
        )
    return proposal


def _collect_registered_source(source_id: int):
    """Registry collection with explicit structured-field precedence."""
    from src.modules.source_registry.collectors import CredentialsRequired, build_collector
    from src.modules.source_registry.models import RegisteredSource

    started = datetime.now(UTC)
    with session_scope() as session:
        source = session.get(RegisteredSource, source_id)
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
        legacy_source = registry_runner._legacy_source(session, source)
        keywords = registry_runner._keywords_for_source(session, source)
        for payload in payloads:
            item = None
            try:
                item, created = registry_runner.upsert_source_item(session, source, payload)
                result.items_created += int(created)
                metadata = dict(payload.raw_payload or {})
                combined_text = "\n".join(part for part in (payload.title, payload.text) if part)
                valid_until = extract_valid_until(str(metadata.get("valid_until") or "")) or extract_valid_until(combined_text)
                if valid_until is not None and valid_until < datetime.now(UTC):
                    item.processing_status = "ignored"
                    item.raw_payload_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
                    result.ignored += 1
                    continue
                signal = _safe_detect_offer_signal(combined_text, keywords)
                if not signal.is_offer:
                    item.processing_status = "ignored"
                    item.raw_payload_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
                    result.ignored += 1
                    continue
                result.offer_signals += 1
                promo_code = (
                    registry_runner._resolve_promko_code(
                        session,
                        source=source,
                        legacy_source=legacy_source,
                        payload=payload,
                        item_created=created,
                        metadata=metadata,
                        result=result,
                    )
                    or str(metadata.get("promo_code") or "").strip()
                    or signal.promo_code
                )
                item.raw_payload_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
                title = (payload.title or payload.text or "Предложение").strip().splitlines()[0][:1000]
                offer_url = str(metadata.get("offer_url") or payload.url or source_url).strip()
                merchant = str(metadata.get("merchant") or source.merchant or "").strip() or None
                brand = str(metadata.get("brand") or source.brand or "").strip() or None
                raw = RawOffer(
                    source_key=legacy_source.key,
                    external_id=payload.external_id or item.content_hash,
                    title=title,
                    source_url=offer_url,
                    merchant=merchant,
                    brand=brand,
                    description=payload.text,
                    conditions=str(metadata.get("conditions") or "").strip() or None,
                    promo_code=promo_code,
                    discount_percent=decimal_or_none(metadata.get("discount_percent")) or signal.discount_percent,
                    discount_amount=decimal_or_none(metadata.get("discount_amount")),
                    old_price=decimal_or_none(metadata.get("old_price")) or signal.old_price,
                    new_price=decimal_or_none(metadata.get("new_price")) or signal.new_price,
                    cashback_percent=decimal_or_none(metadata.get("cashback_percent")),
                    cashback_amount=decimal_or_none(metadata.get("cashback_amount")),
                    delivery_price=decimal_or_none(metadata.get("delivery_price")),
                    max_discount_amount=decimal_or_none(metadata.get("max_discount_amount")),
                    min_order_amount=decimal_or_none(metadata.get("min_order_amount")),
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
                created_offer = source_runner._persist_raw_offer(session, legacy_source, raw)
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


def _render_with_visible_link(offer, publication_format=None) -> str:
    caption = _ORIGINAL_RENDER(offer, publication_format)
    url = str(getattr(offer, "canonical_url", None) or "").strip()
    if not url.startswith(("http://", "https://")):
        return caption
    if "🔗 Ссылка:" in caption:
        return caption
    from html import escape
    return caption + f'\n🔗 Ссылка: <a href="{escape(url, quote=True)}">открыть предложение</a>'


def install_customer_feedback_14() -> None:
    if getattr(registry_runner, _PATCH_MARKER, False):
        return

    registry_service.detect_offer_signal = _safe_detect_offer_signal
    registry_runner.detect_offer_signal = _safe_detect_offer_signal
    GenericWebCollector._raw_offer_payload = staticmethod(_structured_payload)
    PromokoodAdapter.parse = _promokood_parse
    TelegramPublicCollector.collect = _telegram_collect
    source_runner.extract_conditions = _better_conditions
    assisted_setup._promokood_category_proposal = _quality_guarded_promokood_proposal
    registry_runner.collect_registered_source = _collect_registered_source
    telegram_render.render_offer_caption = _render_with_visible_link
    setattr(registry_runner, _PATCH_MARKER, True)


_ORIGINAL_DETECT = registry_service.detect_offer_signal
_ORIGINAL_TELEGRAM_COLLECT = TelegramPublicCollector.collect
_ORIGINAL_CONDITIONS = source_runner.extract_conditions
_ORIGINAL_PROMOKOOD_PROPOSAL = assisted_setup._promokood_category_proposal
_ORIGINAL_RENDER = telegram_render.render_offer_caption
