from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one replacement, got {count}")
    p.write_text(text.replace(old, new), encoding="utf-8")


html_path = "arvectum_data/engine/html_records.py"
replace_once(
    html_path,
    '''    predicate = _is_benefit_heading if anchor_kind == "heading" else (
        lambda node: _has_machine_marker(node) or _is_action_node(node)
    )
''',
    '''    if anchor_kind == "heading":
        predicate = _is_benefit_heading
    elif anchor_kind == "machine":
        predicate = _has_machine_marker
    else:
        predicate = _is_action_node
''',
)
replace_once(
    html_path,
    '''    def _strong_records(self, nodes: Sequence[_Node]) -> list[_AnchoredRecord]:
        machine = [node for node in nodes if _has_machine_marker(node)]
        anchors = machine or [node for node in nodes if _is_action_node(node)]
        kind = "machine" if machine else "action"
        confidence = 0.99 if machine else 0.97
        return self._dedupe_cards(anchors, kind=kind, confidence=confidence)
''',
    '''    def _strong_records(self, nodes: Sequence[_Node]) -> list[_AnchoredRecord]:
        actions = [node for node in nodes if _is_action_node(node)]
        machine = [node for node in nodes if _has_machine_marker(node)]
        action_records = self._dedupe_cards(actions, kind="action", confidence=0.97)
        machine_records = self._dedupe_cards(machine, kind="machine", confidence=0.99)
        result = list(action_records)
        for proposed in machine_records:
            if any(
                proposed.card is existing.card
                or _is_descendant(proposed.card, existing.card)
                or _is_descendant(existing.card, proposed.card)
                for existing in action_records
            ):
                continue
            result.append(proposed)
        return result
''',
)

generic_path = "src/sources/generic_multi_record.py"
replace_once(
    generic_path,
    '''_ACTION_OPEN_RE = re.compile(r"(?:открыть|показать)\\s+(?:промокод|акци\\w*)", re.IGNORECASE)
''',
    '''_ACTION_OPEN_RE = re.compile(r"(?:открыть|open)\\s+(?:промокод|акци\\w*|coupon|promo|deal)", re.IGNORECASE)
_ACTION_SHOW_RE = re.compile(r"(?:показать|show|reveal)\\s+(?:промокод|акци\\w*|coupon|promo|deal)", re.IGNORECASE)
_REVEAL_ACTION_RE = re.compile(r"(?:открыть|показать|open|show|reveal)\\s+(?:промокод|акци\\w*|coupon|promo|deal)", re.IGNORECASE)
''',
)
replace_once(
    generic_path,
    '''_CODE_TOKEN_RE = re.compile(r"^[A-ZА-ЯЁ0-9_-]{4,24}$")
''',
    '''_CODE_TOKEN_RE = re.compile(r"^[A-ZА-ЯЁ0-9_-]{4,24}$")
_CODE_SCAN_RE = re.compile(
    r"\\b(?=[A-ZА-ЯЁ0-9_-]{4,24}\\b)(?=[A-ZА-ЯЁ0-9_-]*\\d|[A-ZА-ЯЁ0-9_-]{5,})([A-ZА-ЯЁ0-9_-]+)\\b"
)
''',
)
replace_once(
    generic_path,
    '''        action_href = _compact(attrs.get("record_action_href")) or None
        anchor_kind = _compact(attrs.get("record_anchor_kind")).casefold()
''',
    '''        action_href = _compact(attrs.get("record_action_href")) or None
        action_text = _compact(attrs.get("record_action_text")) or None
        anchor_kind = _compact(attrs.get("record_anchor_kind")).casefold()
''',
)
replace_once(
    generic_path,
    '''        summary = _SUMMARY_RE.fullmatch(text)
        merchant = self._merchant(text, heading, strong, image_alt, summary)
        title = self._title(text, heading, merchant, summary)
        source_url = urljoin(base_url, href) if href else base_url
        promo_code = self._promo_code(text, strong, data)
''',
    '''        summary = _SUMMARY_RE.fullmatch(text)
        prefer_image_merchant = bool(
            (action_href and "offer_id=" in action_href)
            or (action_text and _ACTION_SHOW_RE.search(action_text))
        )
        merchant = self._merchant(
            text,
            heading,
            strong,
            image_alt,
            summary,
            action_text=action_text,
            prefer_image=prefer_image_merchant,
        )
        title = self._title(text, heading, merchant, summary)
        source_url = urljoin(base_url, href) if href else base_url
        promo_code = self._promo_code(
            text,
            heading,
            strong,
            data,
            suppress_inference=bool(action_text and _REVEAL_ACTION_RE.search(action_text)),
        )
''',
)
replace_once(
    generic_path,
    '''            record_tag=record_tag,
            text=text,
            data=data,
        )
''',
    '''            record_tag=record_tag,
            anchor_kind=anchor_kind,
            action_text=action_text,
            text=text,
            data=data,
        )
''',
)
replace_once(
    generic_path,
    '''    @staticmethod
    def _merchant(
        text: str,
        heading: str | None,
        strong: str | None,
        image_alt: str | None,
        summary: re.Match[str] | None,
    ) -> str | None:
        if image_alt and len(image_alt) <= 120:
            return image_alt
        if strong:
            match = _MERCHANT_FROM_STRONG_RE.search(strong)
            if match:
                value = match.group(1).strip(" .:-—")
                if value:
                    return value[:120]
        if summary:
            value = summary.group(1).strip(" .:-—")
            return value[:120] or None
        benefit = _BENEFIT_RE.search(text)
        if benefit:
            prefix = text[: benefit.start()].strip(" .:-—")
            if prefix and len(prefix) <= 120 and prefix.casefold() != (heading or "").casefold():
                return prefix
        return None
''',
    '''    @staticmethod
    def _merchant(
        text: str,
        heading: str | None,
        strong: str | None,
        image_alt: str | None,
        summary: re.Match[str] | None,
        *,
        action_text: str | None,
        prefer_image: bool,
    ) -> str | None:
        if strong:
            match = _MERCHANT_FROM_STRONG_RE.search(strong)
            if match:
                value = match.group(1).strip(" .:-—")
                if value:
                    return value[:120]
            if (
                len(strong) <= 120
                and not _BENEFIT_RE.search(strong)
                and not _CODE_TOKEN_RE.fullmatch(strong)
                and strong.casefold() != (action_text or "").casefold()
            ):
                return strong
        if (
            heading
            and len(heading) <= 120
            and not _BENEFIT_RE.search(heading)
            and heading.casefold() != (action_text or "").casefold()
        ):
            return heading
        if prefer_image and image_alt and len(image_alt) <= 120:
            return image_alt
        if summary:
            value = summary.group(1).strip(" .:-—")
            return value[:120] or None
        patterns = (
            r"\\b(?:от|для|в)\\s+([A-Za-zА-Яа-яЁё0-9. -]{2,40}?)(?:\\s+на\\s+|\\s+по\\s+|\\s+-?\\d|$)",
            r"^Промокод\\s+([A-Za-zА-Яа-яЁё0-9. -]{2,40}?)\\s+(?:июл|август|сент|на)",
            r"\\bот\\s+([A-Za-zА-Яа-яЁё0-9. -]{2,50})(?:$|[,.!])",
        )
        for pattern in patterns:
            match = re.search(pattern, heading or text, re.IGNORECASE)
            if match:
                value = match.group(1).strip(" .:-—")
                if value:
                    return value[:120]
        benefit = _BENEFIT_RE.search(text)
        if benefit:
            prefix = text[: benefit.start()].strip(" .:-—")
            if prefix and len(prefix) <= 120 and prefix.casefold() != (heading or "").casefold():
                return prefix
        return None
''',
)
replace_once(
    generic_path,
    '''    @staticmethod
    def _promo_code(text: str, strong: str | None, data: dict[str, Any]) -> str | None:
        for key in ("data-promocode", "data-promo-code"):
            value = _compact(str(data.get(key) or ""))
            if value and not re.fullmatch(r"[•*\\s]+", value):
                return value[:120]
        if strong and _CODE_TOKEN_RE.fullmatch(strong) and strong.upper() not in _STOP_CODES:
            return strong
        match = _CODE_AFTER_LABEL_RE.search(text)
        if match:
            value = match.group(1)
            if value.upper() not in _STOP_CODES:
                return value
        return None
''',
    '''    @staticmethod
    def _promo_code(
        text: str,
        heading: str | None,
        strong: str | None,
        data: dict[str, Any],
        *,
        suppress_inference: bool,
    ) -> str | None:
        for key in ("data-promocode", "data-promo-code"):
            value = _compact(str(data.get(key) or ""))
            if value and not re.fullmatch(r"[•*\\s]+", value):
                return value[:120]
        if suppress_inference:
            return None
        if strong and _CODE_TOKEN_RE.fullmatch(strong) and strong.upper() not in _STOP_CODES:
            return strong
        match = _CODE_AFTER_LABEL_RE.search(text)
        if match:
            value = match.group(1)
            if value.upper() not in _STOP_CODES:
                return value
        tail = text
        if heading and text.casefold().startswith(heading.casefold()):
            tail = text[len(heading):]
        for match in _CODE_SCAN_RE.finditer(tail):
            value = match.group(1)
            if value.upper() not in _STOP_CODES:
                return value
        return None
''',
)
replace_once(
    generic_path,
    '''        record_tag: str,
        text: str,
        data: dict[str, Any],
    ) -> str:
''',
    '''        record_tag: str,
        anchor_kind: str,
        action_text: str | None,
        text: str,
        data: dict[str, Any],
    ) -> str:
''',
)
replace_once(
    generic_path,
    '''        if _ACTION_ACTIVATE_RE.search(text):
            return external_id(source_url, merchant, title, promo_code or "")
        if _ACTION_OPEN_RE.search(text):
            return external_id(source_url, merchant, title)
        if promo_code or record_tag == "article":
''',
    '''        if anchor_kind == "heading":
            return external_id(source_url, title, promo_code)
        action_signal = action_text or text
        if _ACTION_ACTIVATE_RE.search(action_signal):
            return external_id(source_url, merchant, title, promo_code or "")
        if _ACTION_SHOW_RE.search(action_signal):
            return external_id(source_url, title)
        if _ACTION_OPEN_RE.search(action_signal):
            return external_id(source_url, merchant, title)
        if promo_code or record_tag == "article":
''',
)
replace_once(
    generic_path,
    '''        usable = bool(result.records) and all(record.status is RecordStatus.READY for record in result.records)
        offers: list[RawOffer] = []
        if usable:
            for record in result.records:
                values = record.values()
                offers.append(
                    RawOffer(
                        source_key=source_key,
                        external_id=str(values["external_id"]),
                        title=str(values["title"]),
                        source_url=str(values["source_url"]),
                        merchant=values.get("merchant"),
                        description=values.get("description"),
                        conditions=values.get("conditions"),
                        promo_code=values.get("promo_code"),
                        discount_percent=values.get("discount_percent"),
                        discount_amount=values.get("discount_amount"),
                        cashback_percent=values.get("cashback_percent"),
                        image_url=values.get("image_url"),
                        valid_until=values.get("valid_until"),
                        raw_payload={
                            "text": values.get("description"),
                            "dp_engine": {
                                "decoder": "generic_multi_record",
                                "record_id": record.record_id,
                                "record_provider": record.boundary.provider,
                                "record_source_ref": record.boundary.source_ref,
                            },
                        },
                    )
                )
        else:
            for record in result.records:
                if record.status is not RecordStatus.READY:
                    warnings.append(f"record_not_ready:{record.record_id}:{record.status.value}")
''',
    '''        ready_records = tuple(record for record in result.records if record.status is RecordStatus.READY)
        usable = bool(ready_records)
        offers: list[RawOffer] = []
        seen_external_ids: set[str] = set()
        for record in ready_records:
            values = record.values()
            external_id_value = str(values["external_id"])
            if external_id_value in seen_external_ids:
                warnings.append(f"duplicate_record_identity:{record.record_id}")
                continue
            seen_external_ids.add(external_id_value)
            offers.append(
                RawOffer(
                    source_key=source_key,
                    external_id=external_id_value,
                    title=str(values["title"]),
                    source_url=str(values["source_url"]),
                    merchant=values.get("merchant"),
                    description=values.get("description"),
                    conditions=values.get("conditions"),
                    promo_code=values.get("promo_code"),
                    discount_percent=values.get("discount_percent"),
                    discount_amount=values.get("discount_amount"),
                    cashback_percent=values.get("cashback_percent"),
                    image_url=values.get("image_url"),
                    valid_until=values.get("valid_until"),
                    raw_payload={
                        "text": values.get("description"),
                        "dp_engine": {
                            "decoder": "generic_multi_record",
                            "record_id": record.record_id,
                            "record_provider": record.boundary.provider,
                            "record_source_ref": record.boundary.source_ref,
                        },
                    },
                )
            )
        for record in result.records:
            if record.status is not RecordStatus.READY:
                warnings.append(f"record_not_ready:{record.record_id}:{record.status.value}")
''',
)

tests_path = "tests/dp_engine/test_semantic_html_records.py"
replace_once(
    tests_path,
    '''    assert attrs["record_anchor_kind"] == "machine"
''',
    '''    assert attrs["record_anchor_kind"] == "action"
''',
)
tests = Path(tests_path)
existing = tests.read_text(encoding="utf-8")
addition = r'''


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
'''
if "test_action_and_machine_evidence_on_one_card_are_not_duplicated" not in existing:
    tests.write_text(existing + addition, encoding="utf-8")

Path("tests/dp_engine/test_live_semantic_remediation.py").write_text(
    r'''from __future__ import annotations

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
''',
    encoding="utf-8",
)
