from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from src.modules.source_registry.collectors import COLLECTORS, build_collector
from src.modules.source_registry.models import RegisteredSource, SourceCandidate, SourceItem, SourceKeyword
from src.modules.source_registry.seed import TELEGRAM_TEST_SOURCES, seed_registry
from src.modules.source_registry.service import (
    ItemPayload,
    add_keyword,
    create_source,
    detect_offer_signal,
    review_candidate,
    upsert_candidate,
    upsert_source_item,
)
from src.shared.config import get_settings
from src.shared.db import Base, create_session, get_engine, reset_db_runtime


@pytest.fixture
def registry_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path / "sources.yaml"
    config.write_text(
        "sources:\n"
        "  - key: promokood\n"
        "    name: Promokood\n"
        "    adapter: promokood\n"
        "    base_url: https://promokood.ru/\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DP_DATABASE_URL", f"sqlite:///{tmp_path / 'registry.db'}")
    monkeypatch.setenv("DP_SOURCES_CONFIG_PATH", str(config))
    get_settings.cache_clear()
    reset_db_runtime()
    Base.metadata.create_all(get_engine())
    try:
        yield config
    finally:
        reset_db_runtime()
        get_settings.cache_clear()


def test_registry_seed_is_idempotent(registry_db: Path) -> None:
    with create_session() as session:
        first = seed_registry(session, sources_config_path=str(registry_db))
        session.commit()
    with create_session() as session:
        second = seed_registry(session, sources_config_path=str(registry_db))
        session.commit()
        sources = session.scalars(select(RegisteredSource)).all()
        keywords = session.scalars(select(SourceKeyword)).all()
    assert first["sources_created"] == 1 + len(TELEGRAM_TEST_SOURCES)
    assert second["sources_created"] == 0
    assert len(sources) == 1 + len(TELEGRAM_TEST_SOURCES)
    assert len([row for row in sources if row.collector_type == "legacy_adapter"]) == 1
    telegram = [row for row in sources if row.platform == "telegram"]
    assert len(telegram) == 5
    assert all(row.collector_type == "telegram_public" for row in telegram)
    assert all(row.network_policy == "auto" for row in telegram)
    assert keywords


def test_registry_seed_keeps_user_selected_collector(registry_db: Path) -> None:
    with create_session() as session:
        seed_registry(session, sources_config_path=str(registry_db))
        source = session.scalar(select(RegisteredSource).where(RegisteredSource.key == "promokood"))
        assert source is not None
        source.collector_type = "generic_web"
        source.item_selector = ".coupon-card"
        session.commit()

    with create_session() as session:
        seed_registry(session, sources_config_path=str(registry_db))
        source = session.scalar(select(RegisteredSource).where(RegisteredSource.key == "promokood"))
        assert source is not None
        assert source.collector_type == "generic_web"
        assert source.item_selector == ".coupon-card"


def test_source_item_upsert_uses_external_id(registry_db: Path) -> None:
    with create_session() as session:
        source = create_source(
            session,
            name="Shop Telegram",
            platform="telegram",
            url="https://t.me/example",
            external_id="example",
            collector_type="telegram_public",
            trust_level="official",
        )
        first, first_created = upsert_source_item(session, source, ItemPayload(external_id="example/100", url="https://t.me/example/100", title=None, text="Скидка 20%"))
        second, second_created = upsert_source_item(session, source, ItemPayload(external_id="example/100", url="https://t.me/example/100", title=None, text="Скидка 25%"))
        session.commit()
        count = len(session.scalars(select(SourceItem)).all())
    assert first.id == second.id
    assert first_created is True
    assert second_created is False
    assert count == 1
    assert second.text == "Скидка 25%"


def test_offer_signal_requires_meaningful_evidence() -> None:
    positive = detect_offer_signal("Только сегодня скидка 25% по промокоду SALE25")
    neutral = detect_offer_signal("Новый обзор коллекции и история бренда")
    assert positive.is_offer is True
    assert positive.discount_percent == 25
    assert positive.promo_code == "SALE25"
    assert neutral.is_offer is False


def test_negative_keyword_reduces_weak_signal(registry_db: Path) -> None:
    with create_session() as session:
        positive = add_keyword(session, "акция", kind="positive")
        negative = add_keyword(session, "обзор", kind="negative")
        signal = detect_offer_signal("Большой обзор: акция бренда", [positive, negative])
    assert signal.is_offer is False


def test_candidate_approval_creates_registered_source(registry_db: Path) -> None:
    with create_session() as session:
        candidate = upsert_candidate(session, platform="rutube", url="https://rutube.ru/channel/123/", name="Shop channel", merchant="Shop", confidence=0.8)
        source = review_candidate(session, candidate.id, "approved", trust_level="official")
        session.commit()
        candidate_after = session.get(SourceCandidate, candidate.id)
    assert source is not None
    assert source.platform == "rutube"
    assert source.collector_type == "rutube_public"
    assert candidate_after.status == "approved"


def test_collectors_registry_has_supported_nonlegacy_types() -> None:
    for collector_type in ("generic_web", "public_page", "telegram_public", "vk_api", "rutube_public"):
        assert collector_type in COLLECTORS
        assert build_collector(collector_type) is not None
