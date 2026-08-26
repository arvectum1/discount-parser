from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from sqlalchemy import text

from src.modules.source_registry.manual_profile import generalize_container_selector, relative_field_selector
from src.modules.source_registry.profile_schema import ensure_source_profile_tables
from src.shared.db import create_session, session_scope


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FollowProfile:
    crawl_mode: str = "direct"
    listing_item_selector: str | None = None
    detail_link_selector: str | None = None
    detail_url_contains: str | None = None
    merchant_selector: str | None = None
    max_detail_pages: int = 100


def get_follow_profile(source_id: int | None) -> FollowProfile:
    if source_id is None:
        return FollowProfile()
    try:
        with create_session() as session:
            ensure_source_profile_tables(session)
            session.commit()
            row = session.execute(
                text(
                    "SELECT crawl_mode, listing_item_selector, detail_link_selector, detail_url_contains, merchant_selector, max_detail_pages "
                    "FROM source_follow_profiles WHERE registered_source_id = :source_id"
                ),
                {"source_id": int(source_id)},
            ).mappings().first()
    except Exception as exc:
        logger.warning("follow_profile_read_failed source_id=%s error=%s", source_id, type(exc).__name__)
        return FollowProfile()
    if not row:
        return FollowProfile()
    mode = str(row.get("crawl_mode") or "direct").strip()
    if mode not in {"direct", "follow_internal"}:
        mode = "direct"
    return FollowProfile(
        crawl_mode=mode,
        listing_item_selector=str(row.get("listing_item_selector") or "").strip() or None,
        detail_link_selector=str(row.get("detail_link_selector") or "").strip() or None,
        detail_url_contains=str(row.get("detail_url_contains") or "").strip() or None,
        merchant_selector=str(row.get("merchant_selector") or "").strip() or None,
        max_detail_pages=max(1, min(int(row.get("max_detail_pages") or 100), 500)),
    )


def set_follow_profile(
    source_id: int,
    *,
    crawl_mode: str,
    listing_item_selector: str | None = None,
    detail_link_selector: str | None = None,
    detail_url_contains: str | None = None,
    merchant_selector: str | None = None,
    max_detail_pages: int = 100,
) -> None:
    mode = (crawl_mode or "direct").strip()
    if mode not in {"direct", "follow_internal"}:
        raise ValueError("crawl_mode must be direct or follow_internal")
    sample_listing = (listing_item_selector or "").strip()
    normalized_listing = generalize_container_selector(sample_listing) if sample_listing else None
    normalized_link = relative_field_selector(sample_listing, detail_link_selector) if sample_listing else (detail_link_selector or "").strip() or None
    contains = (detail_url_contains or "").strip() or None
    merchant = (merchant_selector or "").strip() or None
    limit = max(1, min(int(max_detail_pages or 100), 500))
    if mode == "follow_internal" and not normalized_link:
        raise ValueError("Для перехода по внутренним страницам нужен selector кнопки/ссылки.")

    with session_scope() as session:
        ensure_source_profile_tables(session)
        exists = session.execute(
            text("SELECT registered_source_id FROM source_follow_profiles WHERE registered_source_id = :source_id"),
            {"source_id": int(source_id)},
        ).first()
        params = {
            "source_id": int(source_id),
            "mode": mode,
            "listing": normalized_listing,
            "link": normalized_link,
            "contains": contains,
            "merchant": merchant,
            "limit": limit,
        }
        if exists:
            session.execute(
                text(
                    "UPDATE source_follow_profiles SET crawl_mode=:mode, listing_item_selector=:listing, "
                    "detail_link_selector=:link, detail_url_contains=:contains, merchant_selector=:merchant, max_detail_pages=:limit, "
                    "updated_at=CURRENT_TIMESTAMP WHERE registered_source_id=:source_id"
                ),
                params,
            )
        else:
            session.execute(
                text(
                    "INSERT INTO source_follow_profiles "
                    "(registered_source_id, crawl_mode, listing_item_selector, detail_link_selector, detail_url_contains, merchant_selector, max_detail_pages, created_at, updated_at) "
                    "VALUES (:source_id, :mode, :listing, :link, :contains, :merchant, :limit, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                params,
            )


def extract_internal_detail_urls(
    soup,
    *,
    entry_url: str,
    profile: FollowProfile,
) -> list[str]:
    if profile.crawl_mode != "follow_internal":
        return []
    if not profile.detail_link_selector:
        raise ValueError("two-stage source requires detail_link_selector")

    entry = urlparse(entry_url)
    entry_host = (entry.hostname or "").casefold().removeprefix("www.")
    if not entry_host:
        raise ValueError("entry URL has no hostname")

    if profile.listing_item_selector:
        try:
            containers = soup.select(profile.listing_item_selector)
        except Exception as exc:
            raise ValueError(f"invalid listing CSS selector: {exc}") from exc
        candidates = []
        for container in containers:
            try:
                target = container if profile.detail_link_selector == ":scope" else container.select_one(profile.detail_link_selector)
            except Exception as exc:
                raise ValueError(f"invalid detail-link CSS selector: {exc}") from exc
            if target is not None:
                candidates.append(target)
    else:
        try:
            candidates = soup.select(profile.detail_link_selector)
        except Exception as exc:
            raise ValueError(f"invalid detail-link CSS selector: {exc}") from exc

    result: list[str] = []
    seen: set[str] = set()
    for target in candidates:
        href = str(target.get("href") or "").strip()
        if not href:
            continue
        absolute = urljoin(entry_url, href)
        parsed = urlparse(absolute)
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        if parsed.scheme not in {"http", "https"} or host != entry_host:
            continue
        if profile.detail_url_contains and profile.detail_url_contains not in absolute:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        result.append(absolute)
        if len(result) >= profile.max_detail_pages:
            break
    return result
