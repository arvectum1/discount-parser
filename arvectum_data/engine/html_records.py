from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Mapping, Sequence

from .models import Evidence, FieldSpec, RawAsset
from .records import RecordBoundary, RecordProviderResult, make_record_id

_OFFER_SIGNAL_RE = re.compile(
    r"(?:скид\w*|промокод\w*|к[еэ]шб\w*|бонус\w*|бесплат\w*|подар\w*|сертификат\w*|"
    r"coupon\w*|promo\w*|discount\w*|cashback\w*|bonus\w*|sale\w*|gift\w*|"
    r"\bдо\s*\d{1,3}\s*%|[-−]\s*\d[\d\s]{0,8}\s*(?:₽|руб)|\d{1,3}\s*%)",
    re.IGNORECASE,
)
_ACTION_RE = re.compile(
    r"(?:открыть|показать|активировать|получить|применить|использовать|скопировать|"
    r"open|show|reveal|get|apply|use|copy)\s+(?:промокод\w*|код\b|акци\w*|coupon\w*|promo\w*|deal\w*)",
    re.IGNORECASE,
)
_BENEFIT_HEADING_RE = re.compile(
    r"(?:скид\w*|промокод\w*|дешевле|подар\w*|бонус\w*|к[еэ]шб\w*|бесплат\w*|"
    r"discount\w*|coupon\w*|promo\w*|cashback\w*|bonus\w*|free\b|gift\w*)",
    re.IGNORECASE,
)
_MACHINE_DATA_KEYS = {"data-coupon-id", "data-promocode", "data-promo-code"}
_PROMO_VALUE_DATA_KEYS = {"data-promocode", "data-promo-code"}
_VALIDITY_MARKER_RE = re.compile(
    r"(?:срок\s+действия|остал(?:ось|ись)\s+(?:дн|час)|действует\s+до|активен\s+ещ[её]|valid\s+until|expires?)",
    re.IGNORECASE,
)
_CONTROL_ACTION_RE = re.compile(
    r"(?:добавить|предложить|разместить|submit|add|share)\s+(?:свой\s+)?"
    r"(?:промокод\w*|купон\w*|скидк\w*|offer\w*|promo\w*|coupon\w*|deal\w*)",
    re.IGNORECASE,
)
_CONTACT_TEXT_RE = re.compile(
    r"(?:@[A-Za-z0-9_.-]+|[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|"
    r"[A-Za-z0-9.-]+\.[A-Za-z]{2,})$",
    re.IGNORECASE,
)
_SEMANTIC_TOKENS = {
    "offer",
    "promo",
    "coupon",
    "deal",
    "discount",
    "card",
    "item",
    "product",
    "promotion",
}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


@dataclass(slots=True)
class _Node:
    tag: str
    attrs: dict[str, str]
    path: str
    parent: _Node | None = None
    children: list[_Node] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    child_counts: dict[str, int] = field(default_factory=dict)

    def text(self) -> str:
        parts = list(self.text_parts)
        for child in self.children:
            text = child.text()
            if text:
                parts.append(text)
        return _compact(" ".join(parts))

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def first(self, tags: set[str]) -> _Node | None:
        for node in self.walk():
            if node.tag in tags:
                return node
        return None


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {}, "document")
        self._stack = [self.root]

    @property
    def current(self) -> _Node:
        return self._stack[-1]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        parent = self.current
        index = parent.child_counts.get(tag, 0) + 1
        parent.child_counts[tag] = index
        node = _Node(
            tag=tag,
            attrs={str(key).casefold(): str(value or "") for key, value in attrs},
            path=f"{parent.path}/{tag}[{index}]",
            parent=parent,
        )
        parent.children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self._stack[-1].tag == tag.casefold() and tag.casefold() not in _VOID_TAGS:
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        value = _compact(data)
        if value:
            self.current.text_parts.append(value)


def _semantic_tokens(node: _Node) -> set[str]:
    raw = " ".join(
        node.attrs.get(key, "")
        for key in ("class", "id", "role", "itemtype", "itemprop")
    )
    return {token.casefold() for token in re.findall(r"[A-Za-z0-9_-]+", raw)}


def _is_descendant(node: _Node, ancestor: _Node) -> bool:
    current = node.parent
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


def _first_text(node: _Node, tags: set[str]) -> str | None:
    found = node.first(tags)
    if found is None:
        return None
    value = found.text()
    return value or None


def _first_link(node: _Node) -> tuple[str | None, str | None]:
    target = node if node.tag == "a" else node.first({"a"})
    if target is None:
        return None, None
    href = target.attrs.get("href") or None
    text = target.text() or None
    return href, text


def _parent_link(node: _Node) -> tuple[str | None, str | None]:
    current: _Node | None = node
    while current is not None and current.tag != "document":
        if current.tag == "a" and current.attrs.get("href"):
            return current.attrs.get("href") or None, current.text() or None
        current = current.parent
    return None, None


def _first_image(node: _Node) -> tuple[str | None, str | None]:
    image = node.first({"img"})
    if image is None:
        return None, None
    src = (
        image.attrs.get("src")
        or image.attrs.get("data-src")
        or image.attrs.get("data-lazy-src")
        or None
    )
    alt = image.attrs.get("alt") or None
    return src, alt


def _data_attributes(node: _Node, *, limit: int = 24) -> dict[str, str]:
    result: dict[str, str] = {}
    for current in node.walk():
        for key, value in current.attrs.items():
            if not key.startswith("data-") or not value or key in result:
                continue
            result[key] = value[:1000]
            if len(result) >= limit:
                return result
    return result


def _has_machine_marker(node: _Node) -> bool:
    return any(node.attrs.get(key) for key in _MACHINE_DATA_KEYS)


def _has_promo_value_marker(node: _Node) -> bool:
    return any(node.attrs.get(key) for key in _PROMO_VALUE_DATA_KEYS)


def _is_navigation_node(node: _Node) -> bool:
    current: _Node | None = node
    while current is not None:
        if current.tag in {"nav", "header", "footer", "aside"}:
            return True
        current = current.parent
    return False


def _is_action_node(node: _Node) -> bool:
    if node.tag not in {"a", "button"}:
        return False
    href = node.attrs.get("href", "")
    if "offer_id=" in href:
        return True
    text = node.text()
    if _is_navigation_node(node) or _CONTROL_ACTION_RE.search(text):
        return False
    if node.tag == "a" and href.casefold().startswith(("javascript:", "mailto:", "tel:")):
        return False
    if _CONTACT_TEXT_RE.fullmatch(text):
        return False
    if _ACTION_RE.search(text):
        return True
    # A bare benefit-labelled button is usually a filter/category control.
    # Benefit-only fallback is therefore link-only; explicit buttons above remain
    # valid offer actions.
    if node.tag == "button":
        return False
    # Some production sites label the business link with the benefit itself
    # rather than an imperative verb. Keep this generic and bounded.
    return bool(text and len(text) <= 280 and _OFFER_SIGNAL_RE.search(text) and href)


def _offer_id_value(node: _Node) -> str | None:
    if node.tag != "a":
        return None
    match = re.search(r"(?:[?&])offer_id=([^&#]+)", node.attrs.get("href", ""), re.IGNORECASE)
    return match.group(1) if match else None


def _is_offer_id_action(node: _Node) -> bool:
    return _offer_id_value(node) is not None


def _is_linked_benefit_heading(node: _Node) -> bool:
    if not _is_benefit_heading(node):
        return False
    href, _ = _parent_link(node)
    return bool(href)


def _is_collection_heading(node: _Node) -> bool:
    """Identify a wrapper heading that labels several sibling offers.

    A real offer heading may share a card with one reveal/action. A non-linked
    heading whose surrounding container immediately exposes several independent
    offer actions is instead collection chrome (for example ``active discounts``
    above a list of merchant links) and must not become its own business record.
    """

    parent = node.parent
    if parent is None or parent.tag in {"article", "li"}:
        return False
    if _parent_link(node)[0]:
        return False
    sibling_actions = 0
    for sibling in parent.children:
        if sibling is node:
            continue
        for current in sibling.walk():
            if _is_action_node(current):
                sibling_actions += 1
                if sibling_actions > 1:
                    return True
    return False


def _is_benefit_heading(node: _Node) -> bool:
    return (
        node.tag in {"h2", "h3", "h4"}
        and not _is_navigation_node(node)
        and not _CONTROL_ACTION_RE.search(node.text())
        and not _is_collection_heading(node)
        and bool(_BENEFIT_HEADING_RE.search(node.text()))
    )


def _first_benefit_heading_text(node: _Node) -> str | None:
    for current in node.walk():
        if _is_benefit_heading(current):
            value = current.text()
            if value:
                return value
    return None


def _count_descendants(node: _Node, predicate) -> int:
    return sum(1 for current in node.walk() if predicate(current))


def _machine_unit_identity(current: _Node) -> str | None:
    coupon = _compact(current.attrs.get("data-coupon-id", ""))
    if coupon:
        return f"coupon:{coupon}"
    promo = _compact(current.attrs.get("data-promocode", "") or current.attrs.get("data-promo-code", ""))
    if promo:
        return f"promo:{promo}"
    return None


def _unique_coupon_identity_count(node: _Node) -> int:
    # One DOM node may expose both coupon-id and promo value for the same offer.
    # Prefer coupon-id when present, otherwise promo value. Repeated copies of the
    # same identity stay one unit; sibling distinct promo values split a wrapper.
    values = {value for current in node.walk() if (value := _machine_unit_identity(current))}
    if values:
        return len(values)
    return _count_descendants(node, _has_machine_marker)


def _has_multi_machine_ancestor(node: _Node) -> bool:
    current = node.parent
    while current is not None and current.tag not in {"document", "html", "body"}:
        if _unique_coupon_identity_count(current) > 1:
            return True
        current = current.parent
    return False


def _unique_offer_id_count(node: _Node) -> int:
    return len({value for current in node.walk() if (value := _offer_id_value(current))})


def _is_explicit_action_node(node: _Node) -> bool:
    if node.tag not in {"a", "button"} or _is_offer_id_action(node):
        return False
    return (
        bool(_ACTION_RE.search(node.text()))
        and not _is_navigation_node(node)
        and not _CONTROL_ACTION_RE.search(node.text())
    )


def _is_benefit_action_node(node: _Node) -> bool:
    return (
        _is_action_node(node)
        and not _is_offer_id_action(node)
        and not _is_explicit_action_node(node)
    )


def _contains_multiple_offer_units(node: _Node) -> bool:
    """Return True when one container visibly spans more than one offer unit.

    Repeated DOM representations of the same strong identity are one offer: a
    card may copy ``data-coupon-id`` onto both its wrapper and reveal button, or
    repeat the same ``offer_id`` in nested links. Distinct identity values still
    stop boundary growth. We keep weaker action/heading signals conservative and
    count their DOM occurrences because they are not guaranteed unique.
    """

    return any(
        count > 1
        for count in (
            _unique_coupon_identity_count(node),
            _unique_offer_id_count(node),
            _count_descendants(node, _is_explicit_action_node),
            _count_descendants(node, _is_benefit_action_node),
            _count_descendants(node, _is_benefit_heading),
        )
    )


def _bounded_card(anchor: _Node, *, anchor_kind: str, max_chars: int = 2200) -> _Node:
    """Resolve one strong offer anchor to a cross-signal-bounded card.

    The boundary may contain one machine marker, one explicit action, one
    benefit-labelled link and one benefit heading for the same offer, but it
    never crosses an ancestor that repeats any strong signal category.
    """

    del anchor_kind
    fallback = anchor
    current = anchor.parent
    while current is not None and current.tag not in {"document", "html", "body"}:
        text = current.text()
        if not text or len(text) > max_chars:
            current = current.parent
            continue
        if _contains_multiple_offer_units(current):
            break
        fallback = current
        if current.tag in {"article", "li"}:
            return current
        # Validity/status text is a strong generic card-boundary marker used by
        # promotion pages across layouts. Stop at the nearest single-offer
        # ancestor instead of absorbing adjacent cards.
        if _VALIDITY_MARKER_RE.search(text):
            return current
        current = current.parent
    return fallback


def _fallback_score(node: _Node, text: str) -> tuple[float, int] | None:
    if _is_navigation_node(node):
        return None
    if len(text) < 6 or not _OFFER_SIGNAL_RE.search(text):
        return None
    if node.tag == "article":
        return 0.92, 4
    if node.tag == "li":
        return 0.89, 3
    if node.tag in {"div", "section"} and _semantic_tokens(node) & _SEMANTIC_TOKENS:
        return 0.87, 3
    if node.tag == "a" and node.attrs.get("href"):
        return 0.84, 2
    return None


@dataclass(frozen=True, slots=True)
class _AnchoredRecord:
    confidence: float
    card: _Node
    anchor: _Node
    anchor_kind: str


class SemanticHTMLRecordProvider:
    """Propose record-sized HTML slices without host-specific selectors.

    Strong structural anchors are preferred in this order: machine-readable
    coupon/promo attributes, explicit offer actions, then benefit headings.
    Broad semantic containers are only a last-resort fallback. This keeps
    navigation lists and wrappers containing several offers from being emitted
    as business records while preserving automatic discovery on unknown sites.
    """

    name = "semantic_html_records"

    def __init__(
        self,
        *,
        max_records: int = 300,
        max_text_chars: int = 12_000,
        min_confidence: float = 0.80,
    ) -> None:
        if max_records < 1:
            raise ValueError("max_records must be positive")
        if max_text_chars < 100:
            raise ValueError("max_text_chars must be >= 100")
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        self.max_records = max_records
        self.max_text_chars = max_text_chars
        self.min_confidence = min_confidence

    def records(
        self,
        asset: RawAsset,
        fields: Sequence[FieldSpec],
    ) -> RecordProviderResult:
        del fields
        if not asset.html:
            return RecordProviderResult()

        parser = _TreeParser()
        parser.feed(asset.html)
        parser.close()
        nodes = tuple(node for node in parser.root.walk() if node is not parser.root)

        anchored = self._mixed_records(nodes)
        if not anchored:
            anchored = self._fallback_records(nodes)

        anchored.sort(key=lambda item: item.card.path)
        warnings: list[str] = []
        if len(anchored) > self.max_records:
            warnings.append(f"max_records:{self.max_records}")
            anchored = anchored[: self.max_records]

        boundaries: list[RecordBoundary] = []
        for ordinal, proposed in enumerate(anchored):
            confidence, node, anchor, anchor_kind = (
                proposed.confidence,
                proposed.card,
                proposed.anchor,
                proposed.anchor_kind,
            )
            text = node.text()
            source_ref = node.path
            record_id = make_record_id(asset.asset_id, self.name, source_ref)
            href, link_text = _first_link(node)
            action_href = anchor.attrs.get("href") or None if anchor.tag in {"a", "button"} else None
            action_text = anchor.text() or None
            if anchor_kind == "heading" and not action_href:
                action_href, _ = _parent_link(anchor)
            image_src, image_alt = _first_image(node)
            attributes: Mapping[str, object] = {
                "record_text": text[: self.max_text_chars],
                "record_tag": node.tag,
                "record_attrs": dict(node.attrs),
                "record_heading": _first_benefit_heading_text(node) or _first_text(node, {"h1", "h2", "h3", "h4"}),
                "record_strong": _first_text(node, {"strong", "b"}),
                "record_href": href,
                "record_link_text": link_text,
                "record_action_href": action_href,
                "record_action_text": action_text,
                "record_anchor_kind": anchor_kind,
                "record_image_src": image_src,
                "record_image_alt": image_alt,
                "record_data": _data_attributes(node),
            }
            child = RawAsset(
                asset_id=f"{asset.asset_id}#{record_id}",
                source_url=asset.source_url,
                text=text[: self.max_text_chars],
                attributes=attributes,
                metadata={
                    "record_parent_asset_id": asset.asset_id,
                    "record_provider": self.name,
                    "record_source_ref": source_ref,
                    **dict(asset.metadata),
                },
            )
            boundaries.append(
                RecordBoundary(
                    record_id=record_id,
                    asset=child,
                    provider=self.name,
                    source_ref=source_ref,
                    ordinal=ordinal,
                    confidence=confidence,
                    evidence=(
                        Evidence(
                            kind="semantic_html_record_boundary",
                            source_ref=source_ref,
                            excerpt=text[:500],
                            metadata={"tag": node.tag, "anchor_kind": anchor_kind},
                        ),
                    ),
                    metadata={"tag": node.tag, "anchor_kind": anchor_kind},
                )
            )
        return RecordProviderResult(records=tuple(boundaries), warnings=tuple(warnings))

    def _mixed_records(self, nodes: Sequence[_Node]) -> list[_AnchoredRecord]:
        """Collect independent record signals per card instead of per page.

        Real pages may mix machine coupon ids, offer-id links, explicit actions
        and linked benefit headings. A page-wide winner discards valid records.
        Here every generic signal proposes a bounded card; proposals resolving to
        the exact same structural card are arbitrated by evidence strength while
        distinct cards survive for the strict parity gate to validate.
        """

        groups: tuple[tuple[int, str, float, Sequence[_Node]], ...] = (
            # Explicit promo values are stronger than routing/reveal controls: the
            # value-bearing node identifies the individual promotion while a
            # surrounding action may span several promotions. URL offer IDs remain
            # strongest when present because they are already record-specific.
            (12, "action", 0.99, [node for node in nodes if _is_offer_id_action(node)]),
            # A value-bearing action is already the individual card anchor and
            # preserves action semantics. Promo-only machine nodes outrank only
            # surrounding/broad actions.
            (11, "action", 0.995, [
                node for node in nodes
                if _has_promo_value_marker(node) and _is_action_node(node)
            ]),
            (10, "machine", 0.995, [
                node for node in nodes
                if _has_promo_value_marker(node) and not _is_action_node(node)
            ]),
            (9, "heading", 0.97, [node for node in nodes if _is_linked_benefit_heading(node)]),
            (8, "action", 0.96, [
                node for node in nodes
                if _is_action_node(node)
                and not _is_offer_id_action(node)
                and not _has_promo_value_marker(node)
                and not _has_multi_machine_ancestor(node)
            ]),
            (7, "heading", 0.95, [
                node for node in nodes if _is_benefit_heading(node) and not _is_linked_benefit_heading(node)
            ]),
            (6, "machine", 0.99, [
                node for node in nodes if _has_machine_marker(node) and not _has_promo_value_marker(node)
            ]),
        )
        proposed: list[tuple[int, _AnchoredRecord]] = []
        for priority, kind, confidence, anchors in groups:
            proposed.extend(
                (priority, record)
                for record in self._dedupe_cards(anchors, kind=kind, confidence=confidence)
            )

        # Exact structural-card arbitration only. Ancestor/descendant proposals are
        # deliberately left visible: collapsing them without proof can hide a real
        # sibling offer. Duplicate business identities are still deduplicated later,
        # and DP-016 exact parity remains the final adoption authority.
        by_path: dict[str, tuple[int, _AnchoredRecord]] = {}
        for priority, record in proposed:
            previous = by_path.get(record.card.path)
            if previous is None or priority > previous[0]:
                by_path[record.card.path] = (priority, record)
        return [item[1] for item in by_path.values()]

    def _fallback_records(self, nodes: Sequence[_Node]) -> list[_AnchoredRecord]:
        proposed: list[tuple[int, float, _Node]] = []
        for node in nodes:
            scored = _fallback_score(node, node.text())
            if scored is None:
                continue
            confidence, priority = scored
            if confidence >= self.min_confidence:
                proposed.append((priority, confidence, node))
        proposed.sort(key=lambda item: (-item[0], item[2].path))
        selected: list[_AnchoredRecord] = []
        for _priority, confidence, node in proposed:
            if any(_is_descendant(node, chosen.card) for chosen in selected):
                continue
            selected.append(_AnchoredRecord(confidence, node, node, "fallback"))
        return selected

    @staticmethod
    def _dedupe_cards(
        anchors: Sequence[_Node],
        *,
        kind: str,
        confidence: float,
    ) -> list[_AnchoredRecord]:
        result: list[_AnchoredRecord] = []
        seen: set[str] = set()
        for anchor in anchors:
            card = _bounded_card(anchor, anchor_kind=kind)
            if card.path in seen:
                continue
            seen.add(card.path)
            result.append(_AnchoredRecord(confidence, card, anchor, kind))
        return result
