from arvectum_data.engine import (
    AutoDiscoveryProvider,
    ExtractionEngine,
    FieldSpec,
    FieldStatus,
    RawAsset,
)


def engine():
    return ExtractionEngine([AutoDiscoveryProvider()])


def test_jsonld_discovers_exact_semantic_key():
    html = (
        '<script type="application/ld+json">'
        '{"name":"Widget","price":199}'
        "</script>"
    )
    result = engine().extract(
        RawAsset("a1", html=html),
        [FieldSpec("name"), FieldSpec("price")],
    )

    assert result.values() == {"name": "Widget", "price": 199}
    assert result.decisions["price"].selected.evidence[0].kind == "jsonld"


def test_corroborating_jsonld_and_meta_merge_into_one_candidate():
    html = """
    <meta property="price" content="199">
    <script type="application/ld+json">{"price":"199"}</script>
    """
    result = engine().extract(
        RawAsset("a1", html=html),
        [FieldSpec("price")],
    )

    decision = result.decisions["price"]
    assert decision.status is FieldStatus.AUTO_SELECTED
    assert len(decision.candidates) == 1
    assert decision.selected.confidence == 0.99
    assert {e.kind for e in decision.selected.evidence} == {
        "jsonld",
        "html_meta",
    }


def test_conflicting_structured_values_require_confirmation():
    html = """
    <meta property="price" content="201">
    <script type="application/ld+json">{"price":"199"}</script>
    """
    result = engine().extract(
        RawAsset("a1", html=html),
        [FieldSpec("price")],
    )

    decision = result.decisions["price"]
    assert decision.status is FieldStatus.NEEDS_CONFIRMATION
    assert len(decision.candidates) == 2
    assert decision.selected.value == "199"


def test_dt_dd_alias_is_discovered_without_selector_configuration():
    html = "<dl><dt>Цена</dt><dd>199 ₽</dd></dl>"
    result = engine().extract(
        RawAsset("a1", html=html),
        [FieldSpec("price", aliases=("Цена",))],
    )

    assert result.values() == {"price": "199 ₽"}
    assert (
        result.decisions["price"].selected.evidence[0].kind
        == "html_label_value"
    )


def test_table_label_value_pair_is_discovered():
    html = (
        "<table><tr><th>Article number</th>"
        "<td>SKU-42</td></tr></table>"
    )
    result = engine().extract(
        RawAsset("a1", html=html),
        [FieldSpec("sku", aliases=("Article number",))],
    )

    assert result.values() == {"sku": "SKU-42"}


def test_plain_text_fallback_requires_confirmation_by_default():
    result = engine().extract(
        RawAsset("a1", text="Цена: 199 ₽"),
        [FieldSpec("price", aliases=("Цена",))],
    )

    decision = result.decisions["price"]
    assert decision.status is FieldStatus.NEEDS_CONFIRMATION
    assert decision.selected.value == "199 ₽"
    assert decision.selected.confidence == 0.70


def test_script_and_style_text_are_not_visible_text_candidates():
    html = (
        "<style>Price: 1</style>"
        "<script>Price: 2</script>"
        "<p>Nothing useful</p>"
    )
    result = engine().extract(
        RawAsset("a1", html=html),
        [FieldSpec("price")],
    )

    assert result.decisions["price"].status is FieldStatus.UNRESOLVED


def test_document_title_is_low_confidence_generic_signal():
    html = "<html><head><title>Example page</title></head></html>"
    result = engine().extract(
        RawAsset("a1", html=html),
        [FieldSpec("title", min_confidence=0.70)],
    )

    assert result.values() == {"title": "Example page"}


def test_malformed_jsonld_does_not_block_other_discovery_sources():
    html = """
    <meta name="sku" content="ABC-1">
    <script type="application/ld+json">{"broken": </script>
    """
    result = engine().extract(
        RawAsset("a1", html=html),
        [FieldSpec("sku")],
    )

    assert result.values() == {"sku": "ABC-1"}
    assert result.provider_errors == {}


def test_itemprop_value_is_discovered():
    html = '<meta itemprop="availability" content="InStock">'
    result = engine().extract(
        RawAsset("a1", html=html),
        [FieldSpec("availability")],
    )

    assert result.values() == {"availability": "InStock"}


def test_field_aliases_are_semantic_not_css_selectors():
    html = '<div class="price">199</div>'
    result = engine().extract(
        RawAsset("a1", html=html),
        [FieldSpec("price", aliases=("Цена",))],
    )

    assert result.decisions["price"].status is FieldStatus.UNRESOLVED
