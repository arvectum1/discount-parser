from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Mapping, Sequence

from .models import Evidence, FieldSpec, RawAsset
from .records import RecordBoundary, RecordProviderResult, make_record_id

_OFFER_SIGNAL_RE = re.compile(
    r"(?:скид\w*|промокод\w*|к[еэ]шб\w*|бонус\w*|бесплат\w*|"
    r"coupon\w*|promo\w*|discount\w*|cashback\w*|bonus\w*|sale\w*|"
    r"\bдо\s*\d{1,3}\s*%|[-−]\s*\d[\d\s]{0,8}\s*(?:₽|руб)|\d{1,3}\s*%)",
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


def _score(node: _Node, text: str) -> tuple[float, int] | None:
    if len(text) < 6 or not _OFFER_SIGNAL_RE.search(text):
        return None
    if node.tag == "article":
        return 0.96, 4
    if node.tag == "li":
        return 0.93, 3
    if node.tag in {"div", "section"} and _semantic_tokens(node) & _SEMANTIC_TOKENS:
        return 0.91, 3
    if node.tag == "a" and node.attrs.get("href"):
        return 0.88, 2
    return None


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


class SemanticHTMLRecordProvider:
    """Propose record-sized HTML slices from generic structure and offer semantics.

    The provider has no host names, selectors, XPath expressions, or site profiles.
    It recognizes bounded semantic containers (article/li/semantic card-like divs)
    and offer-like links, preferring the enclosing record container over nested
    actions. Field interpretation remains the responsibility of candidate providers.
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
        del fields  # Structural segmentation is deliberately field-agnostic.
        if not asset.html:
            return RecordProviderResult()

        parser = _TreeParser()
        parser.feed(asset.html)
        parser.close()

        proposed: list[tuple[int, float, _Node, str]] = []
        for node in parser.root.walk():
            if node is parser.root:
                continue
            text = node.text()
            scored = _score(node, text)
            if scored is None:
                continue
            confidence, priority = scored
            if confidence < self.min_confidence:
                continue
            proposed.append((priority, confidence, node, text))

        # Prefer a meaningful enclosing card over its nested action link. Equal
        # priorities stay document-ordered through their structural paths.
        proposed.sort(key=lambda item: (-item[0], item[2].path))
        selected: list[tuple[float, _Node, str]] = []
        for _priority, confidence, node, text in proposed:
            if any(_is_descendant(node, chosen) for _, chosen, _ in selected):
                continue
            selected.append((confidence, node, text))

        selected.sort(key=lambda item: item[1].path)
        warnings: list[str] = []
        if len(selected) > self.max_records:
            warnings.append(f"max_records:{self.max_records}")
            selected = selected[: self.max_records]

        boundaries: list[RecordBoundary] = []
        for ordinal, (confidence, node, text) in enumerate(selected):
            source_ref = node.path
            record_id = make_record_id(asset.asset_id, self.name, source_ref)
            href, link_text = _first_link(node)
            image_src, image_alt = _first_image(node)
            attributes: Mapping[str, object] = {
                "record_text": text[: self.max_text_chars],
                "record_tag": node.tag,
                "record_attrs": dict(node.attrs),
                "record_heading": _first_text(node, {"h1", "h2", "h3", "h4"}),
                "record_strong": _first_text(node, {"strong", "b"}),
                "record_href": href,
                "record_link_text": link_text,
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
                            metadata={"tag": node.tag},
                        ),
                    ),
                    metadata={"tag": node.tag},
                )
            )
        return RecordProviderResult(records=tuple(boundaries), warnings=tuple(warnings))
