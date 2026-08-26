from __future__ import annotations

from sqlalchemy import select, text

from src.modules.source_registry.assisted_setup import AssistedSourceProposal
from src.modules.source_registry.manual_profile import generalize_container_selector, relative_field_selector
from src.modules.source_registry.models import RegisteredSource
from src.modules.source_registry.profile_schema import ensure_source_profile_tables
from src.modules.source_registry.service import create_source, set_source_enabled, update_source
from src.shared.db import session_scope


def _follow_values(proposal: AssistedSourceProposal) -> dict[str, object]:
    mode = (proposal.crawl_mode or "direct").strip()
    if mode not in {"direct", "follow_internal"}:
        raise ValueError("unsupported automatic crawl mode")
    sample_listing = (proposal.listing_item_selector or "").strip()
    listing = generalize_container_selector(sample_listing) if sample_listing else None
    detail = (
        relative_field_selector(sample_listing, proposal.detail_link_selector)
        if sample_listing
        else (proposal.detail_link_selector or "").strip() or None
    )
    if mode == "follow_internal" and not detail:
        raise ValueError("automatic catalogue profile has no internal detail link")
    return {
        "mode": mode,
        "listing": listing,
        "detail": detail,
        "contains": (proposal.detail_url_contains or "").strip() or None,
        "limit": 100,
    }


def apply_assisted_proposal(
    proposal: AssistedSourceProposal,
    *,
    source_id: int | None = None,
    name: str = "",
) -> int:
    """Persist source + image + follow profile as one transaction.

    The preview and confirmation path must never report that the source was not
    changed while having committed only part of the configuration. Auxiliary
    profile tables are also created idempotently for upgraded customer DBs.
    """
    follow = _follow_values(proposal)
    requested_name = name.strip()
    with session_scope() as session:
        ensure_source_profile_tables(session)

        source: RegisteredSource | None
        explicit_existing = source_id is not None
        if explicit_existing:
            source = session.get(RegisteredSource, int(source_id))
            if source is None:
                raise ValueError("Источник не найден")
        else:
            source = session.scalar(select(RegisteredSource).where(RegisteredSource.url == proposal.url))

        values: dict[str, object] = {
            "platform": "website",
            "source_type": "promotion_page",
            "url": proposal.url,
            "collector_type": "generic_web",
            "item_selector": proposal.item_selector,
            "title_selector": proposal.title_selector,
            "promo_code_selector": proposal.promo_code_selector,
            "promo_code_attribute": proposal.promo_code_attribute,
            "conditions_selector": proposal.conditions_selector,
            "valid_until_selector": proposal.valid_until_selector,
            "link_selector": proposal.link_selector,
            "reveal_selector": None,
            "reveal_code_attribute": None,
        }
        # Reconfiguring an explicitly selected existing source must not rename a
        # customer-defined label merely because auto-analysis inferred another
        # site name. A supplied name still wins. URL-based create/update keeps
        # the historical proposal-name behavior.
        if requested_name:
            values["name"] = requested_name
        elif not explicit_existing:
            values["name"] = proposal.name

        if source is None:
            source = create_source(
                session,
                name=requested_name or proposal.name,
                platform="website",
                source_type="promotion_page",
                url=proposal.url,
                collector_type="generic_web",
                trust_level="unknown",
                priority=50,
                check_interval_minutes=120,
                enabled=False,
                item_selector=proposal.item_selector,
                title_selector=proposal.title_selector,
                promo_code_selector=proposal.promo_code_selector,
                promo_code_attribute=proposal.promo_code_attribute,
                conditions_selector=proposal.conditions_selector,
                valid_until_selector=proposal.valid_until_selector,
                link_selector=proposal.link_selector,
            )
        else:
            source = update_source(session, int(source.id), **values)

        actual_id = int(source.id)
        image_params = {
            "source_id": actual_id,
            "selector": (proposal.image_selector or "").strip() or None,
            "attribute": (proposal.image_attribute or "").strip() or None,
        }
        session.execute(
            text(
                """
                INSERT INTO source_image_profiles
                    (registered_source_id, image_selector, image_attribute, created_at, updated_at)
                VALUES (:source_id, :selector, :attribute, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(registered_source_id) DO UPDATE SET
                    image_selector=excluded.image_selector,
                    image_attribute=excluded.image_attribute,
                    updated_at=CURRENT_TIMESTAMP
                """
            ),
            image_params,
        )
        session.execute(
            text(
                """
                INSERT INTO source_follow_profiles
                    (registered_source_id, crawl_mode, listing_item_selector, detail_link_selector,
                     detail_url_contains, merchant_selector, max_detail_pages, created_at, updated_at)
                VALUES (:source_id, :mode, :listing, :detail, :contains, NULL, :limit, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(registered_source_id) DO UPDATE SET
                    crawl_mode=excluded.crawl_mode,
                    listing_item_selector=excluded.listing_item_selector,
                    detail_link_selector=excluded.detail_link_selector,
                    detail_url_contains=excluded.detail_url_contains,
                    merchant_selector=excluded.merchant_selector,
                    max_detail_pages=excluded.max_detail_pages,
                    updated_at=CURRENT_TIMESTAMP
                """
            ),
            {"source_id": actual_id, **follow},
        )
        set_source_enabled(session, actual_id, True)
        return actual_id
