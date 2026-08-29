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


def _is_action_node(node: _Node) -> bool:
    if node.tag not in {"a", "button"}:
        return False
    href = node.attrs.get("href", "")
    if "offer_id=" in href:
        return True
    return bool(_ACTION_RE.search(node.text()))


def _is_benefit_heading(node: _Node) -> bool:
    return node.tag in {"h2", "h3", "h4"} and bool(_BENEFIT_HEADING_RE.search(node.text()))


def _count_descendants(node: _Node, predicate) -> int:
    return sum(1 for current in node.walk() if predicate(current))


def _bounded_card(anchor: _Node, *, anchor_kind: str, max_chars: int = 2200) -> _Node:
    """Resolve one strong offer anchor to a bounded record container.

    We never cross an ancestor that contains multiple anchors of the same strong
    kind. This prevents a page/list wrapper from becoming one record and keeps
    navigation/promotional index noise out of the record set.
    """

    predicate = _is_benefit_heading if anchor_kind == "heading" else (
        lambda node: _has_machine_marker(node) or _is_action_node(node)
    )
    fallback = anchor
    current = anchor.parent
    while current is not None and current.tag not in {"document", "html", "body"}:
        text = current.text()
        if not text or len(text) > max_chars:
            current = current.parent
            continue
        if _count_descendants(current, predicate) > 1:
            break
        fallback = current
        if current.tag in {"article", "li"}:
            return current
        current = current.parent
    return fallback


def _fallback_score(node: _Node, text: str) -> tuple[float, int] | None:
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

        anchored = self._strong_records(nodes)
        if not anchored:
            anchored = self._heading_records(nodes)
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
                "record_heading": _first_text(node, {"h1", "h2", "h3", "h4"}),
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

    def _strong_records(self, nodes: Sequence[_Node]) -> list[_AnchoredRecord]:
        machine = [node for node in nodes if _has_machine_marker(node)]
        anchors = machine or [node for node in nodes if _is_action_node(node)]
        kind = "machine" if machine else "action"
        confidence = 0.99 if machine else 0.97
        return self._dedupe_cards(anchors, kind=kind, confidence=confidence)

    def _heading_records(self, nodes: Sequence[_Node]) -> list[_AnchoredRecord]:
        anchors = [node for node in nodes if _is_benefit_heading(node)]
        return self._dedupe_cards(anchors, kind="heading", confidence=0.94)

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
