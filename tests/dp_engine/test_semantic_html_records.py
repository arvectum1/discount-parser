from __future__ import annotations

from arvectum_data.engine import RawAsset, SemanticHTMLRecordProvider


def _records(html: str, *, max_records: int = 300):
    provider = SemanticHTMLRecordProvider(max_records=max_records)
    return provider.records(
        RawAsset(asset_id="page-1", source_url="https://example.test/offers", html=html),
        (),
    )


def test_prefers_article_and_li_over_nested_offer_actions() -> None:
    result = _records(
        """
        <article><h3>Скидка 20% на заказ</h3><a href='/a'>Показать промокод</a></article>
        <ul><li><a href='/b'>Магазин Скидка 300 ₽ Активировать промокод</a></li></ul>
        """
    )
    assert len(result.records) == 2
    assert [record.asset.attributes["record_tag"] for record in result.records] == ["article", "li"]
    assert result.records[0].asset.attributes["record_href"] == "/a"
    assert result.records[1].asset.attributes["record_href"] == "/b"


def test_offer_like_links_become_independent_records_without_card_container() -> None:
    result = _records(
        """
        <section>
          <a href='/one'>Alpha до 70%</a>
          <a href='/two'>Beta до 25%</a>
          <a href='/about'>О компании</a>
        </section>
        """
    )
    assert len(result.records) == 2
    assert [record.asset.attributes["record_href"] for record in result.records] == ["/one", "/two"]
    assert all(record.asset.attributes["record_tag"] == "a" for record in result.records)


def test_semantic_div_card_requires_offer_signal() -> None:
    result = _records(
        """
        <div class='offer'><h3>Кешбэк 12% за заказ</h3></div>
        <div class='card'><h3>Контакты компании</h3></div>
        """
    )
    assert len(result.records) == 1
    assert result.records[0].asset.attributes["record_heading"] == "Кешбэк 12% за заказ"


def test_structural_record_ids_are_stable_and_do_not_include_business_values() -> None:
    first = _records("<article><h3>Скидка 10%</h3></article>").records[0]
    second = _records("<article><h3>Скидка 99%</h3></article>").records[0]
    assert first.record_id == second.record_id
    assert first.source_ref == second.source_ref


def test_data_attributes_and_image_metadata_are_exposed_generically() -> None:
    result = _records(
        """
        <article>
          <h3>Скидка 15%</h3>
          <img src='/logo.png' alt='Shop'>
          <button data-coupon-id='42' data-promocode='SAVE15'>Получить промокод</button>
        </article>
        """
    )
    attrs = result.records[0].asset.attributes
    assert attrs["record_image_src"] == "/logo.png"
    assert attrs["record_image_alt"] == "Shop"
    assert attrs["record_data"]["data-coupon-id"] == "42"
    assert attrs["record_data"]["data-promocode"] == "SAVE15"


def test_max_records_is_bounded_and_reported() -> None:
    result = _records(
        "".join(f"<a href='/{index}'>Shop {index} до 10%</a>" for index in range(5)),
        max_records=2,
    )
    assert len(result.records) == 2
    assert result.warnings == ("max_records:2",)


def test_non_offer_page_produces_no_records() -> None:
    result = _records("<main><h1>О компании</h1><p>Новости и контакты</p></main>")
    assert result.records == ()
