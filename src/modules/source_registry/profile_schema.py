from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def ensure_source_profile_tables(session: Session) -> None:
    """Keep customer upgrades usable even if an auxiliary profile migration was missed.

    Alembic remains the canonical schema owner. These idempotent CREATE TABLE
    statements are only a runtime safety net for already-installed customer
    databases: DP-CUST-017 evidence showed that automatic analysis could succeed
    and then fail while saving its image/follow profile. Creating the two
    auxiliary tables when absent makes the save path self-healing instead of
    asking the customer to repair the database manually.
    """
    session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS source_image_profiles (
                registered_source_id INTEGER NOT NULL PRIMARY KEY,
                image_selector TEXT NULL,
                image_attribute VARCHAR(120) NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(registered_source_id) REFERENCES registered_sources(id) ON DELETE CASCADE
            )
            """
        )
    )
    session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS source_follow_profiles (
                registered_source_id INTEGER NOT NULL PRIMARY KEY,
                crawl_mode VARCHAR(32) NOT NULL DEFAULT 'direct',
                listing_item_selector TEXT NULL,
                detail_link_selector TEXT NULL,
                detail_url_contains TEXT NULL,
                merchant_selector TEXT NULL,
                max_detail_pages INTEGER NOT NULL DEFAULT 100,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(registered_source_id) REFERENCES registered_sources(id) ON DELETE CASCADE,
                CHECK (crawl_mode IN ('direct','follow_internal')),
                CHECK (max_detail_pages BETWEEN 1 AND 500)
            )
            """
        )
    )
