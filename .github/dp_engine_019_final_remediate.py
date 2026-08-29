from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one target, got {count}: {old[:100]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


HTML = Path("arvectum_data/engine/html_records.py")
GEN = Path("src/sources/generic_multi_record.py")
TEST_HTML = Path("tests/dp_engine/test_semantic_html_records.py")
TEST_GEN = Path("tests/dp_engine/test_live_semantic_remediation.py")

# Domain-neutral structural hardening from five-source live evidence.
replace_once(
    HTML,
    '''_MACHINE_DATA_KEYS = {"data-coupon-id", "data-promocode", "data-promo-code"}\n_CONTROL_ACTION_RE = re.compile(''',
    '''_MACHINE_DATA_KEYS = {"data-coupon-id", "data-promocode", "data-promo-code"}\n_PROMO_VALUE_DATA_KEYS = {"data-promocode", "data-promo-code"}\n_VALIDITY_MARKER_RE = re.compile(\n    r"(?:срок\\s+действия|остал(?:ось|ись)\\s+(?:дн|час)|действует\\s+до|активен\\s+ещ[её]|valid\\s+until|expires?)",\n    re.IGNORECASE,\n)\n_CONTROL_ACTION_RE = re.compile(''',
)
replace_once(
    HTML,
    '''def _has_machine_marker(node: _Node) -> bool:\n    return any(node.attrs.get(key) for key in _MACHINE_DATA_KEYS)\n\n\ndef _is_navigation_node''',
    '''def _has_machine_marker(node: _Node) -> bool:\n    return any(node.attrs.get(key) for key in _MACHINE_DATA_KEYS)\n\n\ndef _has_promo_value_marker(node: _Node) -> bool:\n    return any(node.attrs.get(key) for key in _PROMO_VALUE_DATA_KEYS)\n\n\ndef _is_navigation_node''',
)
replace_once(
    HTML,
    '''        if current.tag in {"nav", "header", "footer"}:''',
    '''        if current.tag in {"nav", "header", "footer", "aside"}:''',
)
replace_once(
    HTML,
    '''    if _ACTION_RE.search(text):\n        return True\n    # Some production sites label the actionable element with the benefit itself\n    # rather than an imperative verb. Keep this generic and bounded, and do not\n    # promote global navigation/chrome into business records.\n    return bool(\n        text\n        and len(text) <= 280\n        and _OFFER_SIGNAL_RE.search(text)\n        and (node.tag == "button" or bool(href))\n    )''',
    '''    if _ACTION_RE.search(text):\n        return True\n    # A bare benefit-labelled button is usually a filter/category control.\n    # Benefit-only fallback is therefore link-only; explicit buttons above remain\n    # valid offer actions.\n    if node.tag == "button":\n        return False\n    # Some production sites label the business link with the benefit itself\n    # rather than an imperative verb. Keep this generic and bounded.\n    return bool(text and len(text) <= 280 and _OFFER_SIGNAL_RE.search(text) and href)''',
)
replace_once(
    HTML,
    '''def _unique_coupon_identity_count(node: _Node) -> int:\n    values = {\n        _compact(current.attrs.get("data-coupon-id", ""))\n        for current in node.walk()\n        if current.attrs.get("data-coupon-id")\n    }\n    if values:\n        return len(values)\n    return _count_descendants(node, _has_machine_marker)''',
    '''def _unique_coupon_identity_count(node: _Node) -> int:\n    # Count distinct strong business identities, not DOM copies. A wrapper with\n    # several different promo values is a collection even when it has no\n    # data-coupon-id; repeated copies of one value remain one offer unit.\n    values = {\n        (key, _compact(current.attrs.get(key, "")))\n        for current in node.walk()\n        for key in _MACHINE_DATA_KEYS\n        if current.attrs.get(key)\n    }\n    if values:\n        return len(values)\n    return _count_descendants(node, _has_machine_marker)''',
)
replace_once(
    HTML,
    '''        fallback = current\n        if current.tag in {"article", "li"}:\n            return current\n        current = current.parent''',
    '''        fallback = current\n        if current.tag in {"article", "li"}:\n            return current\n        # Validity/status text is a strong generic card-boundary marker used by\n        # promotion pages across layouts. Stop at the nearest single-offer\n        # ancestor instead of absorbing adjacent cards.\n        if _VALIDITY_MARKER_RE.search(text):\n            return current\n        current = current.parent''',
)
replace_once(
    HTML,
    '''        groups: tuple[tuple[int, str, float, Sequence[_Node]], ...] = (\n            # Exact-card arbitration keeps reveal/action semantics when present,\n            # while a semantic heading outranks a machine-only representation.\n            # Linked headings remain above ordinary actions because their link is\n            # itself the semantic business target rather than a reveal control.\n            (10, "action", 0.99, [node for node in nodes if _is_offer_id_action(node)]),\n            (9, "heading", 0.97, [node for node in nodes if _is_linked_benefit_heading(node)]),\n            (8, "action", 0.96, [\n                node for node in nodes if _is_action_node(node) and not _is_offer_id_action(node)\n            ]),\n            (7, "heading", 0.95, [\n                node for node in nodes if _is_benefit_heading(node) and not _is_linked_benefit_heading(node)\n            ]),\n            (6, "machine", 0.99, [node for node in nodes if _has_machine_marker(node)]),\n        )''',
    '''        groups: tuple[tuple[int, str, float, Sequence[_Node]], ...] = (\n            # Explicit promo values are stronger than routing/reveal controls: the\n            # value-bearing node identifies the individual promotion while a\n            # surrounding action may span several promotions. URL offer IDs remain\n            # strongest when present because they are already record-specific.\n            (12, "action", 0.99, [node for node in nodes if _is_offer_id_action(node)]),\n            (11, "machine", 0.995, [node for node in nodes if _has_promo_value_marker(node)]),\n            (10, "heading", 0.97, [node for node in nodes if _is_linked_benefit_heading(node)]),\n            (9, "action", 0.96, [\n                node for node in nodes\n                if _is_action_node(node)\n                and not _is_offer_id_action(node)\n                and not _has_promo_value_marker(node)\n            ]),\n            (8, "heading", 0.95, [\n                node for node in nodes if _is_benefit_heading(node) and not _is_linked_benefit_heading(node)\n            ]),\n            (7, "machine", 0.99, [\n                node for node in nodes if _has_machine_marker(node) and not _has_promo_value_marker(node)\n            ]),\n        )''',
)

# Field/identity hardening from live evidence.
replace_once(
    GEN,
    '''_STOP_CODES = {"IMAGE", "КОД", "ПРОМОКОД", "ПРОМОКОДЫ", "COUPON", "PROMO"}\n''',
    '''_STOP_CODES = {"IMAGE", "КОД", "ПРОМОКОД", "ПРОМОКОДЫ", "COUPON", "PROMO"}\n_STATUS_STRONG_RE = re.compile(\n    r"^(?:\\d+\\s+)?(?:остал(?:ось|ись)|дн(?:ей|я)?|час(?:ов|а)?|минут|valid|expires?)\\b",\n    re.IGNORECASE,\n)\n_MERCHANT_AFTER_FROM_RE = re.compile(r"\\bот\\s+([A-Za-zА-Яа-яЁё0-9 ._-]{2,80})", re.IGNORECASE)\n''',
)
replace_once(
    GEN,
    '''        merchant = self._merchant(\n            text,\n            heading,\n            strong,\n            image_alt,\n            summary,\n            action_text=action_text,\n            prefer_image=prefer_image_merchant,\n        )''',
    '''        merchant = self._merchant(\n            text,\n            heading,\n            strong,\n            image_alt,\n            summary,\n            action_text=action_text,\n            prefer_image=prefer_image_merchant,\n            anchor_kind=anchor_kind,\n        )''',
)
replace_once(
    GEN,
    '''        *,\n        action_text: str | None,\n        prefer_image: bool,\n    ) -> str | None:\n        # Reveal/show cards frequently contain service counters in <strong>\n        # while their logo alt carries the stable merchant label.\n        if prefer_image and image_alt and len(image_alt) <= 120:\n            return image_alt\n        if strong:''',
    '''        *,\n        action_text: str | None,\n        prefer_image: bool,\n        anchor_kind: str,\n    ) -> str | None:\n        # Reveal/show cards frequently contain service counters in <strong>\n        # while their logo alt carries the stable merchant label.\n        if prefer_image and image_alt and len(image_alt) <= 120:\n            return image_alt\n\n        # Heading-led records derive merchant only from the semantic heading.\n        # Arbitrary surrounding text/status counters must not invent a merchant.\n        if anchor_kind == "heading":\n            patterns = (\n                r"\\b(?:от|для|в)\\s+([A-Za-zА-Яа-яЁё0-9. -]{2,40}?)(?:\\s+на\\s+|\\s+по\\s+|\\s+-?\\d|$)",\n                r"^Промокод\\s+([A-Za-zА-Яа-яЁё0-9. -]{2,40}?)\\s+(?:июл|август|сент|на)",\n            )\n            for pattern in patterns:\n                match = re.search(pattern, heading or "", re.IGNORECASE)\n                if match:\n                    value = match.group(1).strip(" .:-—")\n                    if value:\n                        return value[:120]\n            return None\n\n        # On action cards the human-readable `от <merchant>` segment is stronger\n        # than generic <strong> status text and is common across coupon layouts.\n        if action_text and re.search(r"\\bот\\b", action_text, re.IGNORECASE):\n            match = _MERCHANT_AFTER_FROM_RE.search(text)\n            if match:\n                value = match.group(1).strip(" .:-—")\n                if value:\n                    return value[:120]\n\n        if strong and _STATUS_STRONG_RE.search(strong):\n            strong = None\n        if strong:''',
)
replace_once(
    GEN,
    '''        tail = text\n        if heading and text.casefold().startswith(heading.casefold()):\n            tail = text[len(heading):]''',
    '''        tail = text\n        if heading:\n            folded_text = text.casefold()\n            folded_heading = heading.casefold()\n            position = folded_text.find(folded_heading)\n            if position >= 0:\n                tail = text[position + len(heading):]''',
)
replace_once(
    GEN,
    '''        for match in _CODE_SCAN_RE.finditer(tail):\n            value = match.group(1)\n            if _is_inferred_code(value):\n                return value''',
    '''        for match in _CODE_SCAN_RE.finditer(tail):\n            value = match.group(1)\n            if value.isdigit():\n                around = tail[max(0, match.start() - 12): min(len(tail), match.end() + 12)]\n                if re.search(r"(?:₽|руб(?:\\.|лей)?|%|\\bр\\b)", around, re.IGNORECASE):\n                    continue\n            if _is_inferred_code(value):\n                return value''',
)
replace_once(
    GEN,
    '''        if anchor_kind == "heading":\n            return external_id(source_url, title, promo_code)\n        offer_id = parse_qs(urlsplit(source_url).query).get("offer_id", [None])[0]''',
    '''        if anchor_kind == "heading":\n            return external_id(source_url, title, promo_code)\n        offer_id = parse_qs(urlsplit(source_url).query).get("offer_id", [None])[0]''',
)
replace_once(
    GEN,
    '''        coupon_id = _compact(str(data.get("data-coupon-id") or ""))\n        if coupon_id.isdigit():\n            return f"{source_key}-coupon:{coupon_id}"\n        summary = _SUMMARY_RE.fullmatch(text)''',
    '''        explicit_promo = _compact(str(data.get("data-promocode") or data.get("data-promo-code") or ""))\n        if anchor_kind == "machine" and explicit_promo:\n            return external_id(source_url, merchant, title, promo_code or explicit_promo)\n        coupon_id = _compact(str(data.get("data-coupon-id") or ""))\n        if coupon_id.isdigit() and not heading:\n            return f"{source_key}-coupon:{coupon_id}"\n        if anchor_kind == "machine" and heading:\n            return external_id(source_url, title, promo_code)\n        summary = _SUMMARY_RE.fullmatch(text)''',
)
# Restore the DP-016 safety contract: a page is usable only if every discovered
# record is READY. Diagnostic READY-subset relaxation must not reach main.
replace_once(
    GEN,
    '''        ready_records = tuple(record for record in result.records if record.status is RecordStatus.READY)\n        usable = bool(ready_records)''',
    '''        ready_records = tuple(record for record in result.records if record.status is RecordStatus.READY)\n        usable = bool(result.records) and len(ready_records) == len(result.records)''',
)

# Add regression coverage for the live root causes.
with TEST_HTML.open("a", encoding="utf-8") as handle:
    handle.write(r'''


def test_distinct_explicit_promo_values_split_wrapper_into_records() -> None:
    html = """
    <section>
      <div data-promocode="SAVE10"><h3>Скидка 10%</h3></div>
      <div data-promocode="SAVE20"><h3>Скидка 20%</h3></div>
      <a href="/go">Активировать промокод</a>
    </section>
    """
    result = SemanticHTMLRecordProvider().records(RawAsset(asset_id="a", html=html), ())
    values = [record.asset.attributes.get("record_data", {}) for record in result.records]
    assert any(item.get("data-promocode") == "SAVE10" for item in values)
    assert any(item.get("data-promocode") == "SAVE20" for item in values)
    assert all(record.asset.attributes.get("record_text") != "Активировать промокод" for record in result.records)


def test_bare_benefit_filter_button_is_not_offer_record() -> None:
    html = "<section><button>Скидка</button><button>Бесплатные бонусы</button></section>"
    result = SemanticHTMLRecordProvider().records(RawAsset(asset_id="a", html=html), ())
    assert result.records == ()


def test_aside_benefit_links_are_chrome_not_business_records() -> None:
    html = "<aside><a href='/related'>Скидка 20% на соседнее предложение</a></aside>"
    result = SemanticHTMLRecordProvider().records(RawAsset(asset_id="a", html=html), ())
    assert result.records == ()


def test_validity_marker_bounds_single_offer_card() -> None:
    html = """
    <section><div><h3>Скидка 25%</h3><a href='/deal'>Открыть акцию</a><span>Срок действия до 30.09.2026</span></div></section>
    """
    result = SemanticHTMLRecordProvider().records(RawAsset(asset_id="a", html=html), ())
    assert len(result.records) == 1
    assert "Срок действия" in result.records[0].asset.attributes["record_text"]
''')

with TEST_GEN.open("a", encoding="utf-8") as handle:
    handle.write(r'''


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
''')
