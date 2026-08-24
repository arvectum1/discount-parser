from __future__ import annotations

from decimal import Decimal

from src.core import offer_structuring as core
from src.customer_feedback_16 import (
    AUTO_READY_THRESHOLD,
    READINESS_VERSION,
    structure_raw_offer_calibrated,
)
from src.modules.source_registry.service import ItemPayload, detect_offer_signal
from src.sources.base import RawOffer


GOOD_TEMPLATES = (
    ("Скидка 20% на первый заказ", "Скидка 20% на первый заказ."),
    ("Промокод SAVE20 на скидку 20%", "Промокод SAVE20 — скидка 20% на заказ."),
    ("Скидка 500 ₽ на заказ", "Скидка 500 ₽ на заказ от 3000 ₽."),
    ("Кэшбэк 10% на покупки", "Кэшбэк 10% на покупки по карте."),
    ("Бесплатная доставка", "Бесплатная доставка при заказе от 1500 ₽."),
    ("Акция: цена снижена", "Акция: было 5 000 ₽, стало 3 500 ₽."),
    ("Промокод WEEKEND25", "Промокод WEEKEND25 для заказа в выходные."),
    ("Скидка 15,5% на ассортимент", "Скидка 15,5% на ассортимент магазина."),
    ("Скидка 1 500 ₽ на первый заказ", "Скидка 1 500 ₽ на первый заказ."),
    ("Кэшбэк 500 ₽ за покупку", "Кэшбэк 500 ₽ за покупку от 5000 ₽."),
)

SOURCE_CONTEXTS = (
    ("website", "https://shop.example/deal"),
    ("promo_aggregator", "https://promos.example/deal"),
    ("telegram", "https://t.me/shop/123"),
    ("vk", "https://vk.com/wall-1_123"),
)

UNSAFE_TEMPLATES = (
    ("Активировать промокод", "Активировать промокод"),
    ("Все промокоды", "Промокод FIRST10 — скидка 10%. Промокод SECOND20 — скидка 20%."),
    ("Скидки магазина", "Скидка 10% на одно. Скидка 20% на другое. Скидка 30% на третье."),
    ("Акция на товары", "Акция: 1 000 ₽, 900 ₽ и 800 ₽ на разные товары."),
    ("Большая распродажа", "Большая распродажа уже началась. Подробности на сайте."),
)


def _batch(*, title: str, text: str, platform: str, url: str):
    payload = ItemPayload(
        external_id=f"{platform}:{abs(hash((title, text, url)))}",
        url=url,
        title=title,
        text=text,
        raw_payload={"collector": "generic_web" if platform == "website" else platform},
    )
    signal = detect_offer_signal("\n".join((title, text)))
    return core.structure_registry_payload(
        payload,
        source_key=f"registry:{platform}",
        source_url=url,
        platform=platform,
        signal=signal,
    )


def test_generic_offer_does_not_need_merchant_or_conditions_to_be_ready() -> None:
    batch = _batch(
        title="Скидка 20% на первый заказ",
        text="Скидка 20% на первый заказ.",
        platform="website",
        url="https://shop.example/deal",
    )

    assert batch.disposition == "processed"
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.auto_ready is True
    assert candidate.confidence >= AUTO_READY_THRESHOLD
    assert candidate.raw.merchant is None
    assert candidate.raw.raw_payload["readiness_version"] == READINESS_VERSION


def test_legacy_raw_offer_uses_same_calibrated_readiness() -> None:
    result = core.structure_raw_offer(
        RawOffer(
            source_key="legacy:test",
            external_id="1",
            title="Скидка 25% на заказ",
            source_url="https://shop.example/deal/1",
            description="Скидка 25% на заказ.",
            discount_percent=Decimal("25"),
        )
    )

    assert core.structure_raw_offer is structure_raw_offer_calibrated
    assert result.auto_ready is True
    assert result.raw.raw_payload["readiness_version"] == READINESS_VERSION


def test_normal_offer_auto_ready_rate_is_at_least_90_percent() -> None:
    batches = [
        _batch(title=title, text=text, platform=platform, url=url)
        for title, text in GOOD_TEMPLATES
        for platform, url in SOURCE_CONTEXTS
    ]
    assert len(batches) == 40

    ready = [batch for batch in batches if batch.disposition == "processed"]
    ready_rate = len(ready) / len(batches)

    assert ready_rate >= 0.90, (
        f"false-review regression: only {len(ready)}/{len(batches)} "
        f"normal offers were auto-ready ({ready_rate:.1%})"
    )


def test_unsafe_offer_auto_ready_rate_is_zero() -> None:
    batches = [
        _batch(title=title, text=text, platform=platform, url=url)
        for title, text in UNSAFE_TEMPLATES
        for platform, url in SOURCE_CONTEXTS
    ]
    assert len(batches) == 20

    unsafe_ready = [batch for batch in batches if batch.disposition == "processed"]
    assert unsafe_ready == []


def test_multiple_promos_remain_quarantined_after_calibration() -> None:
    batch = _batch(
        title="Все промокоды магазина",
        text="Промокод SAVE10 — скидка 10%. Промокод SAVE20 — скидка 20%.",
        platform="website",
        url="https://shop.example/promos",
    )

    assert batch.disposition == "needs_review"
    assert batch.candidates == ()
