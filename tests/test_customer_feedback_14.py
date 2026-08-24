from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from src.core.conditions import extract_conditions
from src.customer_feedback_14 import (
    _advertiser_from_text,
    _extract_promo_code,
    _first_external_offer_url,
    _preview_is_coherent,
    _structured_metadata,
)
from src.modules.offers.models import Offer
from src.modules.source_registry.service import detect_offer_signal
from src.modules.xlsx import service as xlsx_service
from src.sources.adapters.promokood import PromokoodAdapter
from src.sources.base import RawOffer
from src.telegram.publication_format import PublicationFormat
from src.telegram.render import render_offer_caption


AGNI_TEXT = """Сетевые фильтры Agni со скидкой 30%
⚡
Защитите технику дома, на работе или на даче с сетевыми фильтрами Agni.
🔥
AGNISALE0058
— скидка 30% на каждый заказ в магазине Agni на Яндекс Маркете.
Выбирайте подходящий вариант по ссылке
👉
https://agni.prfl.me/promoskid_en/7skaet?erid=2RanykhkWBM
Реклама. Рекламодатель ООО \"АГНИ\", ИНН 6230125975
"""


def test_agni_url_fragment_is_not_promo_code() -> None:
    signal = detect_offer_signal(AGNI_TEXT)
    assert signal.is_offer is True
    assert signal.promo_code == "AGNISALE0058"
    assert signal.promo_code != "SKID_EN"
    assert signal.discount_percent == Decimal("30")


def test_standalone_promo_code_is_extracted_but_url_fragment_is_ignored() -> None:
    assert _extract_promo_code(AGNI_TEXT) == "AGNISALE0058"
    assert _extract_promo_code("Ссылка https://example.test/promoskid_en/path") is None


def test_agni_conditions_are_not_lost() -> None:
    result = extract_conditions(AGNI_TEXT)
    assert result.conditions is not None
    assert "скидка 30%" in result.conditions.lower()
    assert "каждый заказ" in result.conditions.lower()


def test_agni_external_offer_url_and_advertiser_are_preserved() -> None:
    assert _first_external_offer_url(None, AGNI_TEXT) == "https://agni.prfl.me/promoskid_en/7skaet?erid=2RanykhkWBM"
    assert _advertiser_from_text(AGNI_TEXT) == 'ООО "АГНИ"'


def test_promokood_parser_keeps_neighbor_cards_separate_and_rejects_cta_as_code() -> None:
    html = """
    <main>
      <div class="offer-card">
        <strong>ВкусВилл</strong>
        <p>Скидка 200 ₽ на первый заказ от 1000 ₽ до 31.12.2026.</p>
        <div>Промокод: VS3B43</div>
        <button>Активировать промокод</button>
      </div>
      <div class="offer-card">
        <strong>Яндекс Афиша</strong>
        <p>Скидка 10% на заказ от 3000 ₽ до 01.08.2026.</p>
        <div>Промокод: TO437400</div>
        <button>Активировать промокод</button>
      </div>
    </main>
    """
    offers = PromokoodAdapter("https://promokood.ru/o/example").parse(html)
    assert len(offers) == 2
    assert {offer.promo_code for offer in offers} == {"VS3B43", "TO437400"}
    assert all((offer.promo_code or "").casefold() != "активировать" for offer in offers)
    assert all((offer.title or "").casefold() != "активировать промокод" for offer in offers)
    assert "TO437400" not in offers[0].description
    assert "VS3B43" not in offers[1].description


def test_promokood_cta_only_block_is_not_an_offer() -> None:
    html = "<main><button>Активировать промокод</button></main>"
    assert PromokoodAdapter("https://promokood.ru/o/example").parse(html) == []


def test_bad_preview_cannot_be_treated_as_high_confidence() -> None:
    bad = SimpleNamespace(
        title="Активировать промокод",
        promo_code="АКТИВИРОВАТЬ",
        excerpt="Активировать промокод",
    )
    merged = SimpleNamespace(
        title="Скидки магазина",
        promo_code="SAVE100",
        excerpt=(
            "Промокод SAVE100 до 01.01.2027. Промокод SAVE200 до 02.01.2027. "
            "Промокод SAVE300 до 03.01.2027."
        ),
    )
    assert _preview_is_coherent(bad) is False
    assert _preview_is_coherent(merged) is False


def test_known_adapter_metadata_keeps_structured_fields() -> None:
    raw = RawOffer(
        source_key="demo",
        external_id="1",
        title="Скидка",
        source_url="https://example.test/deal",
        merchant="Example Shop",
        conditions="При заказе от 1000 ₽",
        promo_code="SAVE30",
        discount_percent=Decimal("30"),
    )
    metadata = _structured_metadata(raw, "demo")
    assert metadata["structured_fields"] is True
    assert metadata["merchant"] == "Example Shop"
    assert metadata["promo_code"] == "SAVE30"
    assert metadata["discount_percent"] == "30"
    assert metadata["conditions"] == "При заказе от 1000 ₽"


def test_publication_contains_visible_offer_link() -> None:
    offer = Offer(
        title="Скидка 30%",
        offer_type="promo",
        status="ready",
        geo_scope="unknown",
        promo_code="SAVE30",
        canonical_url="https://example.test/deal",
    )
    fmt = PublicationFormat(order=("promo_code",), enabled=frozenset({"promo_code"}))
    caption = render_offer_caption(offer, fmt)
    assert "SAVE30" in caption
    assert "Ссылка на предложение" in caption
    assert "https://example.test/deal" in caption


def test_xlsx_correction_contract_exposes_customer_fields() -> None:
    assert "display_title" in xlsx_service.OFFER_HEADERS
    for field in (
        "display_title",
        "merchant",
        "category",
        "conditions",
        "discount_percent",
        "promo_code",
        "valid_until",
        "canonical_url",
    ):
        assert field in xlsx_service.EDITABLE_COLUMNS
