from __future__ import annotations

from src.sources.adapters.common import external_id
from src.sources.generic_multi_record import GenericMultiRecordOfferDecoder


def test_open_action_prefers_structural_merchant_and_suppresses_fake_code() -> None:
    html = """
    <article>
      <strong>от Demo Shop</strong>
      <h3>Скидка 25% на заказ</h3>
      <a href='/go'>Открыть промокод</a>
    </article>
    """
    result = GenericMultiRecordOfferDecoder().decode(html, page_url="https://example.test/offers", source_key="demo")
    assert result.usable is True
    assert len(result.offers) == 1
    offer = result.offers[0]
    assert offer.merchant == "Demo Shop"
    assert offer.promo_code is None
    assert offer.external_id == external_id(offer.source_url, offer.merchant, offer.title)


def test_show_action_uses_image_merchant_but_identity_does_not_depend_on_it() -> None:
    html = """
    <article>
      <img src='/logo.png' alt='Image Merchant'>
      <h3>Скидка 15% на заказ</h3>
      <a href='/go'>Показать промокод</a>
    </article>
    """
    result = GenericMultiRecordOfferDecoder().decode(html, page_url="https://example.test/offers", source_key="demo")
    offer = result.offers[0]
    assert offer.merchant == "Image Merchant"
    assert offer.promo_code is None
    assert offer.external_id == external_id(offer.source_url, offer.title)


def test_heading_record_uses_tail_code_for_heading_identity() -> None:
    html = """
    <div><a href='/shop'><h3>Промокод Demo Shop на август</h3></a><p>SAVE10 действует на заказ</p></div>
    """
    result = GenericMultiRecordOfferDecoder().decode(html, page_url="https://example.test/offers", source_key="demo")
    offer = result.offers[0]
    assert offer.promo_code == "SAVE10"
    assert offer.external_id == external_id(offer.source_url, offer.title, offer.promo_code)


def test_duplicate_business_identity_from_two_boundaries_is_deduplicated() -> None:
    html = """
    <div><a href='/go'>Открыть промокод</a><strong>от Demo</strong><h3>Скидка 10%</h3></div>
    <div><a href='/go'>Открыть промокод</a><strong>от Demo</strong><h3>Скидка 10%</h3></div>
    """
    result = GenericMultiRecordOfferDecoder().decode(html, page_url="https://example.test/offers", source_key="demo")
    assert result.usable is True
    assert len(result.records.records) == 2
    assert len(result.offers) == 1
    assert any(warning.startswith("duplicate_record_identity:") for warning in result.warnings)
