from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from src.core.offer_structuring import structure_registry_payload
from src.customer_feedback_15 import _persist_with_quality_gate
from src.modules.offers.models import Offer
from src.modules.source_registry import runner as registry_runner
from src.modules.source_registry.models import SourceItem
from src.modules.source_registry.service import ItemPayload, create_source, detect_offer_signal
from src.shared.config import get_settings
from src.shared.db import Base, create_session, get_engine, reset_db_runtime


@pytest.fixture
def structuring_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DP_DATABASE_URL", f"sqlite:///{tmp_path / 'structuring.db'}")
    get_settings.cache_clear()
    reset_db_runtime()
    Base.metadata.create_all(get_engine())
    try:
        yield
    finally:
        reset_db_runtime()
        get_settings.cache_clear()


def _batch(payload: ItemPayload, *, merchant: str | None = None):
    text = "\n".join(part for part in (payload.title, payload.text) if part)
    signal = detect_offer_signal(text)
    return structure_registry_payload(
        payload,
        source_key="registry:test",
        source_url="https://source.example/promos",
        source_merchant=merchant,
        platform="website",
        signal=signal,
    )


def test_agni_feedback_is_structured_from_social_text() -> None:
    payload = ItemPayload(
        external_id="telegram:channel:100",
        url="https://t.me/channel/100",
        title=None,
        text=(
            "Сетевые фильтры Agni со скидкой 30%\n"
            "Промокод AGNISALE0058\n"
            "Скидка 30% на каждый заказ в магазине Agni на Яндекс Маркете.\n"
            "https://agni.prfl.me/promo\n"
            "Рекламодатель: ООО «АГНИ», ИНН 7700000000"
        ),
        raw_payload={"collector": "telegram_public"},
    )

    batch = _batch(payload)

    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.raw.promo_code == "AGNISALE0058"
    assert candidate.raw.discount_percent == Decimal("30")
    assert candidate.raw.merchant == "ООО «АГНИ»"
    assert candidate.raw.source_url == "https://agni.prfl.me/promo"
    assert "Сетевые фильтры" in candidate.raw.title
    assert candidate.accepted is True
    assert candidate.auto_ready is True


def test_url_slug_can_never_become_promo_code() -> None:
    payload = ItemPayload(
        external_id="1",
        url="https://shop.example/promoskid_en/catalog",
        title="Скидка на фильтры",
        text="Скидка 30% на заказ. Промокод AGNISALE0058",
        raw_payload={"collector": "generic_web"},
    )

    candidate = _batch(payload, merchant="Agni").candidates[0]

    assert candidate.raw.promo_code == "AGNISALE0058"
    assert candidate.raw.promo_code != "SKID_EN"


def test_product_code_word_is_not_treated_as_promo_code() -> None:
    payload = ItemPayload(
        external_id="2",
        url="https://shop.example/item/2",
        title="Скидка на товар",
        text="Код товара: ABCD. Сегодня скидка 20% на покупку.",
        raw_payload={"collector": "generic_web"},
    )

    candidate = _batch(payload, merchant="Shop").candidates[0]

    assert candidate.raw.promo_code is None
    assert candidate.raw.discount_percent == Decimal("20")


def test_titleless_item_gets_business_title_not_placeholder() -> None:
    payload = ItemPayload(
        external_id="3",
        url="https://shop.example/deal/3",
        title=None,
        text="Скидка 25% на первый заказ от 3000 рублей. Только для новых клиентов.",
        raw_payload={"collector": "generic_web"},
    )

    candidate = _batch(payload, merchant="Example Shop").candidates[0]

    assert candidate.raw.title != "Предложение"
    assert "Скидка 25%" in candidate.raw.title
    assert candidate.raw.discount_percent == Decimal("25")


def test_multiple_promos_in_one_block_are_quarantined_not_merged() -> None:
    payload = ItemPayload(
        external_id="4",
        url="https://shop.example/promos",
        title="Все акции магазина",
        text=(
            "Промокод SAVE10 — скидка 10% на первый заказ.\n"
            "Промокод SAVE20 — скидка 20% на второй заказ."
        ),
        raw_payload={"collector": "generic_web"},
    )

    batch = _batch(payload, merchant="Example Shop")

    assert batch.candidates == ()
    assert batch.disposition == "needs_review"
    assert "несколько" in (batch.reason or "").casefold()


def test_cta_only_is_quarantined_not_saved_as_offer() -> None:
    payload = ItemPayload(
        external_id="5",
        url="https://shop.example/promos",
        title="Активировать промокод",
        text="Активировать промокод",
        raw_payload={"collector": "generic_web"},
    )

    batch = _batch(payload)

    assert batch.candidates == ()
    assert batch.disposition == "needs_review"


def test_structured_source_fields_have_precedence_over_heuristics() -> None:
    payload = ItemPayload(
        external_id="6",
        url="https://known.example/deal",
        title="Скидка на заказ",
        text="Промокод WRONG10. Скидка 10% на заказ.",
        raw_payload={
            "collector": "known_site_adapter",
            "structured_fields": True,
            "merchant": "Known Shop",
            "promo_code": "RIGHT20",
            "discount_percent": "20",
            "conditions": "Скидка 20% на первый заказ",
            "source_url": "https://known.example/deal",
        },
    )

    candidate = _batch(payload).candidates[0]

    assert candidate.raw.promo_code == "RIGHT20"
    assert candidate.raw.discount_percent == Decimal("20")
    assert candidate.raw.conditions == "Скидка 20% на первый заказ"
    assert candidate.raw.merchant == "Known Shop"
    assert candidate.auto_ready is True


def test_registry_rejects_merged_block_before_offer_persistence(
    structuring_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeCollector:
        def collect(self, source):
            return [
                ItemPayload(
                    external_id="merged-1",
                    url=source.url,
                    title="Все акции",
                    text=(
                        "Промокод FIRST10 — скидка 10%.\n"
                        "Промокод SECOND20 — скидка 20%."
                    ),
                    raw_payload={"collector": "generic_web"},
                )
            ]

    monkeypatch.setattr(registry_runner, "build_collector", lambda collector_type: FakeCollector())

    with create_session() as session:
        source = create_source(
            session,
            name="Merged source",
            platform="website",
            url="https://shop.example/promos",
            collector_type="generic_web",
            merchant="Shop",
        )
        source_id = source.id
        session.commit()

    result = registry_runner.collect_registered_source(source_id)

    with create_session() as session:
        items = session.scalars(select(SourceItem)).all()
        offers = session.scalars(select(Offer)).all()

    assert result.errors == 0
    assert len(items) == 1
    assert items[0].processing_status == "needs_review"
    assert "склеивать" in (items[0].processing_error or "")
    assert offers == []


def test_registry_single_offer_uses_universal_contract(
    structuring_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeCollector:
        def collect(self, source):
            return [
                ItemPayload(
                    external_id="agni-1",
                    url="https://t.me/example/1",
                    title=None,
                    text=(
                        "Сетевые фильтры Agni со скидкой 30%\n"
                        "Промокод AGNISALE0058\n"
                        "Скидка 30% на каждый заказ.\n"
                        "https://agni.prfl.me/promo\n"
                        "Рекламодатель: ООО «АГНИ», ИНН 7700000000"
                    ),
                    raw_payload={"collector": "telegram_public"},
                )
            ]

    monkeypatch.setattr(registry_runner, "build_collector", lambda collector_type: FakeCollector())

    with create_session() as session:
        source = create_source(
            session,
            name="Agni channel",
            platform="telegram",
            url="https://t.me/example",
            external_id="example",
            collector_type="telegram_public",
        )
        source_id = source.id
        session.commit()

    result = registry_runner.collect_registered_source(source_id)

    with create_session() as session:
        item = session.scalar(select(SourceItem))
        offer = session.scalar(select(Offer))

    assert result.errors == 0
    assert offer is not None
    assert offer.promo_code == "AGNISALE0058"
    assert offer.discount_percent == Decimal("30")
    assert offer.merchant == "ООО «АГНИ»"
    assert offer.canonical_url == "https://agni.prfl.me/promo"
    assert item is not None
    assert item.processing_status in {"processed", "needs_review"}


def test_runtime_patch_covers_legacy_and_registry_paths() -> None:
    assert sources_persist_name() == "_persist_with_quality_gate"
    assert registry_runner._persist_raw_offer is _persist_with_quality_gate
    assert registry_runner.collect_registered_source.__name__ == "_collect_registered_source_v15"


def sources_persist_name() -> str:
    from src.sources import runner as sources_runner

    return sources_runner._persist_raw_offer.__name__
