from __future__ import annotations

from arvectum_data.engine import FieldSpec, RawAsset, SemanticHTMLRecordProvider


def _records(html: str):
    asset = RawAsset(asset_id="asset:test", source_url="https://example.test/", html=html)
    return SemanticHTMLRecordProvider().records(asset, (FieldSpec(key="title", required=True),))


def test_article_suppresses_nested_action_duplicate() -> None:
    result = _records(
        """
        <article>
          <h3>Скидка 15% на заказ</h3>
          <a href='/go'>Показать промокод</a>
        </article>
        """
    )
    assert len(result.records) == 1
    assert result.records[0].asset.attributes["record_heading"] == "Скидка 15% на заказ"


def test_list_item_suppresses_nested_action_duplicate() -> None:
    result = _records(
        """
        <ul>
          <li><strong>Магазин</strong><span>Скидка 20%</span><a href='/go'>Открыть промокод</a></li>
        </ul>
        """
    )
    assert len(result.records) == 1
    assert result.records[0].asset.attributes["record_tag"] == "li"


def test_standalone_offer_links_form_independent_records() -> None:
    result = _records(
        """
        <section>
          <a href='/a'>Alpha до 70%</a>
          <a href='/b'>Beta до 25%</a>
        </section>
        """
    )
    assert len(result.records) == 2
    assert [record.asset.attributes["record_href"] for record in result.records] == ["/a", "/b"]


def test_semantic_div_requires_offer_signal() -> None:
    result = _records("<div class='offer card'><span>Просто описание магазина</span></div>")
    assert result.records == ()


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


def test_record_id_is_structural_not_business_value() -> None:
    first = _records("<article><h3>Скидка 10%</h3><a href='/go'>Получить промокод</a></article>")
    second = _records("<article><h3>Скидка 50%</h3><a href='/go'>Получить промокод</a></article>")
    assert first.records[0].record_id == second.records[0].record_id


def test_max_records_is_bounded() -> None:
    html = "<main>" + "".join(f"<a href='/{i}'>Скидка {i + 1}%</a>" for i in range(5)) + "</main>"
    asset = RawAsset(asset_id="asset:test", source_url="https://example.test/", html=html)
    result = SemanticHTMLRecordProvider(max_records=2).records(asset, (FieldSpec(key="title", required=True),))
    assert len(result.records) == 2
    assert "max_records:2" in result.warnings


def test_non_offer_page_has_no_records() -> None:
    assert _records("<main><h2>Каталог</h2><p>Обычный текст</p></main>").records == ()


def test_action_card_can_include_linked_benefit_heading() -> None:
    result = _records(
        """
        <article>
          <a href='/merchant'><h3>Скидка 20% на заказ</h3></a>
          <a href='/go'>Открыть промокод</a>
        </article>
        """
    )
    assert len(result.records) == 1
    attrs = result.records[0].asset.attributes
    assert attrs["record_heading"] == "Скидка 20% на заказ"
    assert attrs["record_action_href"] == "/go"


def test_two_offer_actions_are_not_wrapped_into_one_record() -> None:
    result = _records(
        """
        <section>
          <div><h3>Скидка 20% Alpha</h3><a href='/a'>Открыть промокод</a></div>
          <div><h3>Скидка 30% Beta</h3><a href='/b'>Открыть промокод</a></div>
        </section>
        """
    )
    assert len(result.records) == 2


def test_two_benefit_links_are_not_wrapped_into_one_record() -> None:
    result = _records(
        """
        <section>
          <a href='/a'>Alpha до 70%</a>
          <a href='/b'>Beta до 25%</a>
        </section>
        """
    )
    assert len(result.records) == 2


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


def test_collection_heading_over_multiple_offer_links_is_not_a_record() -> None:
    result = _records(
        """
        <section>
          <h2>Сейчас активны скидки</h2>
          <a href='/a'>Alpha до 70%</a>
          <a href='/b'>Beta до 25%</a>
          <a href='/c'>Gamma до 10%</a>
        </section>
        """
    )
    assert len(result.records) == 3
    assert [record.asset.attributes["record_href"] for record in result.records] == ["/a", "/b", "/c"]
