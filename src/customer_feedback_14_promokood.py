from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup

from src.core.validity import extract_valid_until
from src.sources.adapters.promokood import PromokoodAdapter
from src.sources.base import RawOffer


_PATCHED = False
_FALLBACK_PARSE = PromokoodAdapter.parse
_CODE_TOKEN_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9_-]{2,31}$")
_PROMO_DESCRIPTION_RE = re.compile(r"^промокод\b", re.IGNORECASE)
_STOP_PREFIXES = ("о сервисе", "похожие предложения", "ключевые преимущества", "mcc-коды")


def _tokens(soup: BeautifulSoup) -> list[str]:
    return [" ".join(str(value).split()).strip() for value in soup.stripped_strings if str(value).strip()]


def _merchant(soup: BeautifulSoup, tokens: list[str]) -> str | None:
    for selector in ("h1", "h2"):
        node = soup.find(selector)
        if node:
            value = " ".join(node.get_text(" ", strip=True).split()).strip()
            if value and len(value) <= 255:
                return value
    for value in tokens[:8]:
        lowered = value.casefold()
        if lowered.startswith("активировать") or lowered.startswith("промокод"):
            continue
        if len(value) <= 120:
            return value
    return None


def _is_code_token(value: str) -> bool:
    if not _CODE_TOKEN_RE.fullmatch(value):
        return False
    lowered = value.casefold()
    if lowered in {
        "активировать", "промокод", "промокоды", "скидка", "скидки",
        "открыть", "подробнее", "получить", "применить", "использовать",
    }:
        return False
    return True


def _is_stop(value: str) -> bool:
    lowered = value.casefold().strip(" :")
    return any(lowered.startswith(prefix) for prefix in _STOP_PREFIXES)


def _structured_promokood_parse(self: PromokoodAdapter, html: str) -> list[RawOffer]:
    soup = BeautifulSoup(html, "html.parser")
    values = _tokens(soup)
    merchant = _merchant(soup, values)

    stop_index = len(values)
    for index, value in enumerate(values):
        if _is_stop(value):
            stop_index = index
            break

    offers: list[RawOffer] = []
    seen: set[str] = set()
    index = 0
    while index + 1 < stop_index:
        code = values[index]
        description = values[index + 1]
        if not (_is_code_token(code) and _PROMO_DESCRIPTION_RE.search(description)):
            index += 1
            continue

        parts = [description]
        cursor = index + 2
        while cursor < stop_index and len(parts) < 6:
            current = values[cursor]
            if _is_stop(current):
                break
            if (
                cursor + 1 < stop_index
                and _is_code_token(current)
                and _PROMO_DESCRIPTION_RE.search(values[cursor + 1])
            ):
                break
            if current.casefold().startswith("активировать промокод"):
                break
            parts.append(current)
            if extract_valid_until(current) is not None and current.casefold().startswith("до "):
                cursor += 1
                break
            cursor += 1

        body = " ".join(parts).strip()
        valid_until = extract_valid_until(body)
        condition_parts = [part for part in parts[1:] if extract_valid_until(part) is None]
        conditions = " ".join(condition_parts).strip() or None
        discount_percent = self._discount_percent(body)
        discount_amount = self._discount_amount(body) if discount_percent is None else None
        signature = f"{code.casefold()}|{body.casefold()}"
        if signature in seen:
            index = max(cursor, index + 2)
            continue
        seen.add(signature)

        title_core = description[:1].upper() + description[1:] if description else "Промокод"
        title = f"{merchant} — {title_core}" if merchant else title_core
        external_id = hashlib.sha256(
            f"{self.base_url}|{signature}".encode("utf-8")
        ).hexdigest()[:32]
        offers.append(
            RawOffer(
                source_key=self.key,
                external_id=external_id,
                title=title[:300],
                source_url=self.base_url,
                merchant=merchant,
                description=body[:2000],
                conditions=conditions,
                promo_code=code,
                discount_percent=discount_percent,
                discount_amount=discount_amount,
                valid_until=valid_until,
                raw_payload={
                    "text": body,
                    "promo_code": code,
                    "parser_version": "dp-cust-014-promokood-blocks",
                },
            )
        )
        index = max(cursor, index + 2)

    return offers


def _parse_v14_blocks(self: PromokoodAdapter, html: str) -> list[RawOffer]:
    structured = _structured_promokood_parse(self, html)
    if structured:
        return structured
    return _FALLBACK_PARSE(self, html)


def install_promokood_block_parser() -> None:
    global _PATCHED
    if _PATCHED:
        return
    PromokoodAdapter.parse = _parse_v14_blocks
    _PATCHED = True
