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


def test_heading_identity_beats_nested_coupon_marker() -> None:
    html = """
    <div>
      <a href='/shop'><h3>Промокод Demo Shop на август</h3></a>
      <span data-coupon-id='42'></span>
      <p>SAVE10 действует на заказ</p>
    </div>
    """
    result = GenericMultiRecordOfferDecoder().decode(html, page_url="https://example.test/offers", source_key="demo")
    offer = result.offers[0]
    assert offer.external_id == external_id(offer.source_url, offer.title, offer.promo_code)
    assert offer.external_id != "demo-coupon:42"


def test_url_offer_id_beats_nested_coupon_marker() -> None:
    html = """
    <article>
      <img src='/logo.png' alt='Image Merchant'>
      <h3>Скидка 25% на заказ</h3>
      <span data-coupon-id='42'></span>
      <a href='/go?offer_id=77'>Показать промокод</a>
    </article>
    """
    result = GenericMultiRecordOfferDecoder().decode(html, page_url="https://example.test/offers", source_key="demo")
    offer = result.offers[0]
    assert offer.external_id == "77"
    assert offer.external_id != "demo-coupon:42"


def test_show_action_prefers_image_over_expiry_strong() -> None:
    html = """
    <article>
      <img src='/logo.png' alt='Image Merchant'>
      <strong>241 Осталось дней</strong>
      <h3>Скидка 15% на заказ</h3>
      <a href='/go?offer_id=77'>Показать промокод</a>
    </article>
    """
    result = GenericMultiRecordOfferDecoder().decode(html, page_url="https://example.test/offers", source_key="demo")
    offer = result.offers[0]
    assert offer.merchant == "Image Merchant"
    assert offer.external_id == "77"


def test_inferred_code_rejects_prose_word_after_label() -> None:
    html = """
    <article>
      <h3>Скидка 10% на заказ</h3>
      <p>Промокод получите после перехода на сайт</p>
    </article>
    """
    result = GenericMultiRecordOfferDecoder().decode(html, page_url="https://example.test/offers", source_key="demo")
    assert result.offers[0].promo_code is None


def test_inferred_code_keeps_code_shaped_token_after_label() -> None:
    html = """
    <article>
      <h3>Скидка 10% на заказ</h3>
      <p>Промокод SAVE10 действует на заказ</p>
    </article>
    """
    result = GenericMultiRecordOfferDecoder().decode(html, page_url="https://example.test/offers", source_key="demo")
    assert result.offers[0].promo_code == "SAVE10"



def test_status_strong_does_not_override_action_text_merchant() -> None:
    html = """
    <div><a href='/listing/deal'>390 от Demo Shop Промокод</a><h3>Скидка 390 рублей на покупку</h3><strong>241 Осталось дней</strong></div>
    """
    result = GenericMultiRecordOfferDecoder().decode(html, page_url="https://example.test/store", source_key="demo")
    offer = result.offers[0]
    assert offer.merchant and offer.merchant.startswith("Demo Shop Промокод")
    assert offer.merchant != "241 Осталось дней"
    assert offer.promo_code is None


def test_heading_code_scan_starts_after_heading_even_with_prefix_text() -> None:
    html = """
    <div><span>300 ₽</span><h3>По коду скидка 300 ₽ на XIAOMI</h3><p>MTS59 активен ещё 2 дня</p></div>
    """
    result = GenericMultiRecordOfferDecoder().decode(html, page_url="https://example.test/store", source_key="demo")
    offer = result.offers[0]
    assert offer.promo_code == "MTS59"


def test_explicit_promo_machine_identity_uses_business_fields_not_routing_coupon_id() -> None:
    html = """
    <div data-promocode='SAVE10'><strong>Demo</strong><h3>Скидка 10% на заказ</h3></div>
    """
    result = GenericMultiRecordOfferDecoder().decode(html, page_url="https://example.test/store", source_key="demo")
    offer = result.offers[0]
    assert offer.external_id == external_id(offer.source_url, offer.merchant, offer.title, offer.promo_code)


def test_page_with_non_ready_record_is_not_usable() -> None:
    html = """
    <div data-promocode='SAVE10'><h3>Скидка 10%</h3></div>
    <div data-coupon-id='2'>Промокод</div>
    """
    result = GenericMultiRecordOfferDecoder().decode(html, page_url="https://example.test/store", source_key="demo")
    if any(record.status.value != "ready" for record in result.records.records):
        assert result.usable is False
