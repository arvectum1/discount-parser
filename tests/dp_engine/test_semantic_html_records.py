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
    assert attrs["record_anchor_kind"] == "action"


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


def test_machine_markers_suppress_unrelated_offer_navigation_noise() -> None:
    result = _records(
        """
        <nav><ul><li><a href='/promocodes'>Все промокоды и скидки</a></li></ul></nav>
        <main>
          <article><h3>Скидка 20%</h3><button data-coupon-id='11'>Получить промокод</button></article>
          <article><h3>Скидка 30%</h3><button data-coupon-id='12'>Получить промокод</button></article>
        </main>
        """
    )
    assert len(result.records) == 2
    assert [record.asset.attributes["record_data"]["data-coupon-id"] for record in result.records] == ["11", "12"]


def test_explicit_actions_define_cards_and_preserve_action_href() -> None:
    result = _records(
        """
        <nav><a href='/promo'>Промокоды и скидки</a></nav>
        <div class='offers'>
          <div class='entry'><a href='/shop'>Shop</a><h3>Скидка 10%</h3><a href='/go?offer_id=1'>Показать промокод</a></div>
          <div class='entry'><a href='/shop2'>Shop 2</a><h3>Бонус 500 ₽</h3><a href='/go?offer_id=2'>Открыть промокод</a></div>
        </div>
        """
    )
    assert len(result.records) == 2
    assert [record.asset.attributes["record_action_href"] for record in result.records] == [
        "/go?offer_id=1",
        "/go?offer_id=2",
    ]
    assert all(record.asset.attributes["record_anchor_kind"] == "action" for record in result.records)


def test_broad_wrapper_with_multiple_actions_is_not_one_record() -> None:
    result = _records(
        """
        <section class='offer-list'>
          <div><h3>Скидка 10%</h3><button>Открыть промокод</button></div>
          <div><h3>Скидка 20%</h3><button>Открыть промокод</button></div>
        </section>
        """
    )
    assert len(result.records) == 2
    assert all("offer-list" not in str(record.asset.attributes["record_attrs"].get("class", "")) for record in result.records)


def test_benefit_headings_are_fallback_anchors_when_actions_absent() -> None:
    result = _records(
        """
        <div class='list'>
          <div><a href='/one'><h3>Промокод Shop на август</h3></a><p>Код SAVE10</p></div>
          <div><a href='/two'><h3>Скидка 25% для Shop 2</h3></a><p>Условия акции</p></div>
        </div>
        """
    )
    assert len(result.records) == 2
    assert [record.asset.attributes["record_action_href"] for record in result.records] == ["/one", "/two"]
    assert all(record.asset.attributes["record_anchor_kind"] == "heading" for record in result.records)



def test_action_and_machine_evidence_on_one_card_are_not_duplicated() -> None:
    result = _records(
        """
        <article>
          <h3>Скидка 15%</h3>
          <button data-coupon-id='42' data-promocode='SAVE15'>Получить промокод</button>
        </article>
        """
    )
    assert len(result.records) == 1
    attrs = result.records[0].asset.attributes
    assert attrs["record_anchor_kind"] == "action"
    assert attrs["record_data"]["data-coupon-id"] == "42"
    assert attrs["record_heading"] == "Скидка 15%"


def test_machine_backed_and_action_only_offers_are_both_retained() -> None:
    result = _records(
        """
        <main>
          <article><h3>Скидка 10%</h3><button data-coupon-id='11'>Получить промокод</button></article>
          <article><h3>Скидка 20%</h3><a href='/two'>Открыть промокод</a></article>
        </main>
        """
    )
    assert len(result.records) == 2
    assert [record.asset.attributes["record_heading"] for record in result.records] == [
        "Скидка 10%",
        "Скидка 20%",
    ]


def test_action_card_can_expand_across_nested_machine_marker() -> None:
    result = _records(
        """
        <div class='entry'>
          <h3>Скидка 30% в магазине</h3>
          <div><span data-coupon-id='77'></span><a href='/go'>Получить промокод</a></div>
        </div>
        """
    )
    assert len(result.records) == 1
    attrs = result.records[0].asset.attributes
    assert attrs["record_heading"] == "Скидка 30% в магазине"
    assert attrs["record_action_href"] == "/go"
    assert attrs["record_data"]["data-coupon-id"] == "77"



def test_mixed_page_keeps_linked_heading_and_unrelated_action_record() -> None:
    result = _records(
        """
        <main>
          <a href='/heading'><h3>Промокод Alpha на август</h3><span>SAVE10</span></a>
          <article><h3>Скидка 20% в Beta</h3><a href='/action'>Открыть промокод</a></article>
        </main>
        """
    )
    assert len(result.records) == 2
    kinds = {record.asset.attributes["record_anchor_kind"] for record in result.records}
    assert kinds == {"heading", "action"}
    assert {record.asset.attributes["record_href"] for record in result.records} == {"/heading", "/action"}


def test_offer_word_link_is_action_signal_without_imperative_verb() -> None:
    result = _records(
        """
        <article>
          <strong>Demo Shop</strong>
          <a href='/deal'>Скидка 25% и промокод</a>
        </article>
        """
    )
    assert len(result.records) == 1
    attrs = result.records[0].asset.attributes
    assert attrs["record_anchor_kind"] == "action"
    assert attrs["record_action_href"] == "/deal"



def test_cross_signal_wrapper_does_not_absorb_heading_siblings() -> None:
    result = _records(
        """
        <section class='offer-list'>
          <div class='entry'><a href='/one'><h3>Промокод Alpha 10%</h3></a></div>
          <div class='entry'><a href='/two'><h3>Скидка Beta 20%</h3></a></div>
          <div class='entry'><button>Открыть промокод</button></div>
        </section>
        """
    )
    assert len(result.records) == 3
    assert all(record.asset.attributes["record_tag"] != "section" for record in result.records)


def test_cross_signal_boundary_keeps_one_mixed_offer_together() -> None:
    result = _records(
        """
        <div class='entry'>
          <a href='/shop'><h3>Скидка 25% в Alpha</h3></a>
          <div><span data-coupon-id='77'></span><button>Показать промокод</button></div>
        </div>
        """
    )
    assert len(result.records) == 1
    attrs = result.records[0].asset.attributes
    assert attrs["record_tag"] == "div"
    assert attrs["record_heading"] == "Скидка 25% в Alpha"
    assert attrs["record_data"]["data-coupon-id"] == "77"


def test_machine_offer_cannot_climb_over_multiple_action_siblings() -> None:
    result = _records(
        """
        <main>
          <div class='entry'><span data-coupon-id='1'></span><a href='/one'>Получить промокод</a></div>
          <div class='entry'><a href='/two'>Открыть промокод</a></div>
        </main>
        """
    )
    assert len(result.records) == 2
    assert [record.asset.attributes["record_href"] for record in result.records] == ["/one", "/two"]



def test_repeated_same_coupon_identity_collapses_to_one_semantic_card() -> None:
    result = _records(
        """
        <div class='offer' data-coupon-id='42'>
          <h3>Скидка 20% на заказ</h3>
          <button data-coupon-id='42'>Показать промокод</button>
        </div>
        """
    )
    assert len(result.records) == 1
    attrs = result.records[0].asset.attributes
    assert attrs["record_anchor_kind"] == "action"
    assert attrs["record_heading"] == "Скидка 20% на заказ"
    assert attrs["record_data"]["data-coupon-id"] == "42"


def test_heading_outranks_machine_only_representation_on_same_card() -> None:
    result = _records(
        """
        <div class='offer' data-coupon-id='42'>
          <h3>Скидка 20% на заказ</h3>
        </div>
        """
    )
    assert len(result.records) == 1
    assert result.records[0].asset.attributes["record_anchor_kind"] == "heading"


def test_distinct_coupon_identities_remain_separate_records() -> None:
    result = _records(
        """
        <section>
          <div data-coupon-id='42'><h3>Скидка 20% на Alpha</h3><button data-coupon-id='42'>Показать промокод</button></div>
          <div data-coupon-id='43'><h3>Скидка 30% на Beta</h3><button data-coupon-id='43'>Показать промокод</button></div>
        </section>
        """
    )
    assert len(result.records) == 2
    assert [record.asset.attributes["record_data"]["data-coupon-id"] for record in result.records] == ["42", "43"]


def test_repeated_same_offer_id_does_not_split_one_card() -> None:
    result = _records(
        """
        <div class='offer'>
          <a href='/?offer_id=77#offer-77'>Скидка на заказ</a>
          <a href='/?offer_id=77#offer-77'>Показать промокод</a>
        </div>
        """
    )
    assert len(result.records) == 1
    assert result.records[0].asset.attributes["record_action_href"] == "/?offer_id=77#offer-77"


def test_contact_pseudo_links_and_footer_headings_are_not_offer_records() -> None:
    result = _records(
        """
        <main>
          <a href='mailto:promokodik@example.com'>promokodik@example.com</a>
          <a href='javascript:void(0)'>Промокодов 18</a>
          <h3>Добавить свой промокод</h3>
          <div><h3>Скидка 25% на заказ</h3></div>
        </main>
        <footer><h3>Скидки и предложения</h3></footer>
        """
    )
    assert len(result.records) == 1
    assert result.records[0].asset.attributes["record_heading"] == "Скидка 25% на заказ"
