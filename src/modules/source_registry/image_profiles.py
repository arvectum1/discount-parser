from __future__ import annotations

import logging
import re
from dataclasses import replace
from urllib.parse import urljoin

from sqlalchemy import text

from src.modules.source_registry.collectors import CollectorError, GenericWebCollector
from src.modules.source_registry.profile_schema import ensure_source_profile_tables
from src.modules.source_registry.service import ItemPayload
from src.shared.db import create_session, session_scope


logger = logging.getLogger(__name__)
_PATCH_MARKER = "_dp_fb5_image_profile_patch"


def get_image_profile(source_id: int | None) -> tuple[str | None, str | None]:
    """Return the optional per-source image selector and attribute."""
    if source_id is None:
        return None, None
    try:
        with create_session() as session:
            ensure_source_profile_tables(session)
            session.commit()
            row = session.execute(
                text(
                    "SELECT image_selector, image_attribute "
                    "FROM source_image_profiles WHERE registered_source_id = :source_id"
                ),
                {"source_id": int(source_id)},
            ).mappings().first()
    except Exception as exc:
        logger.warning("image_profile_read_failed source_id=%s error=%s", source_id, type(exc).__name__)
        return None, None
    if not row:
        return None, None
    selector = str(row.get("image_selector") or "").strip() or None
    attribute = str(row.get("image_attribute") or "").strip() or None
    return selector, attribute


def set_image_profile(source_id: int, *, image_selector: str | None, image_attribute: str | None) -> None:
    selector = (image_selector or "").strip() or None
    attribute = (image_attribute or "").strip() or None
    with session_scope() as session:
        ensure_source_profile_tables(session)
        exists = session.execute(
            text("SELECT registered_source_id FROM source_image_profiles WHERE registered_source_id = :source_id"),
            {"source_id": int(source_id)},
        ).first()
        if exists:
            session.execute(
                text(
                    "UPDATE source_image_profiles "
                    "SET image_selector = :selector, image_attribute = :attribute, updated_at = CURRENT_TIMESTAMP "
                    "WHERE registered_source_id = :source_id"
                ),
                {"source_id": int(source_id), "selector": selector, "attribute": attribute},
            )
        else:
            session.execute(
                text(
                    "INSERT INTO source_image_profiles "
                    "(registered_source_id, image_selector, image_attribute, created_at, updated_at) "
                    "VALUES (:source_id, :selector, :attribute, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"source_id": int(source_id), "selector": selector, "attribute": attribute},
            )


def _srcset_first(value: str) -> str:
    first = value.split(",", 1)[0].strip()
    return first.split(None, 1)[0].strip()


def _image_value(node, explicit_attribute: str | None) -> str | None:
    if explicit_attribute:
        value = node.get(explicit_attribute)
        return str(value or "").strip() or None

    for attribute in ("src", "data-src", "data-lazy-src", "data-original", "content"):
        value = str(node.get(attribute) or "").strip()
        if value:
            return value
    for attribute in ("srcset", "data-srcset"):
        value = str(node.get(attribute) or "").strip()
        if value:
            return _srcset_first(value)
    style = str(node.get("style") or "")
    match = re.search(r"background-image\s*:\s*url\(['\"]?([^'\")]+)", style, flags=re.IGNORECASE)
    return match.group(1).strip() if match else None


def extract_image_url(container, *, page_url: str, selector: str | None, attribute: str | None) -> str | None:
    try:
        if selector:
            target = container.select_one(selector)
        else:
            target = container.select_one(
                "img[src], img[data-src], img[data-lazy-src], img[data-original], "
                "img[srcset], source[srcset], [data-src], [data-lazy-src], [style*='background-image']"
            )
    except Exception as exc:
        raise CollectorError(f"invalid image CSS selector: {exc}") from exc
    if target is None:
        return None
    value = _image_value(target, attribute)
    if not value or value.startswith(("data:", "blob:")):
        return None
    return urljoin(page_url, value)


def install_profile_image_extraction() -> None:
    """Extend precise CSS profiles with image extraction without changing legacy adapters."""
    if getattr(GenericWebCollector, _PATCH_MARKER, False):
        return

    original = GenericWebCollector._profile_items

    def _profile_items_with_images(self, source, soup, page_url: str) -> list[ItemPayload]:
        items = original(self, source, soup, page_url)
        if not items or not source.item_selector:
            return items

        selector, attribute = get_image_profile(getattr(source, "id", None))
        try:
            containers = soup.select(source.item_selector)
        except Exception as exc:
            raise CollectorError(f"invalid extraction CSS selector: {exc}") from exc

        eligible = []
        for container in containers[: self.policy.max_items]:
            if " ".join(container.stripped_strings)[:12000]:
                eligible.append(container)

        enriched: list[ItemPayload] = []
        for payload, container in zip(items, eligible, strict=False):
            if payload.image_url:
                enriched.append(payload)
                continue
            image_url = extract_image_url(
                container,
                page_url=page_url,
                selector=selector,
                attribute=attribute,
            )
            enriched.append(replace(payload, image_url=image_url) if image_url else payload)
        if len(items) > len(enriched):
            enriched.extend(items[len(enriched) :])
        return enriched

    GenericWebCollector._profile_items = _profile_items_with_images
    setattr(GenericWebCollector, _PATCH_MARKER, True)
