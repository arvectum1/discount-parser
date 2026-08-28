from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from .models import Candidate, Evidence, FieldSpec, RawAsset


_SPACE_RE = re.compile(r"\s+")
_LABEL_VALUE_RE = re.compile(r"^\s*(.{1,120}?)\s*(?::|—|–|-)\s+(.{1,1000}?)\s*$")


def _clean_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value).strip()


def _semantic_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _canonical_value(value: Any) -> str:
    if isinstance(value, str):
        value = _clean_text(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class _Signal:
    semantic_name: str
    value: Any
    confidence: float
    evidence: Evidence


class _DiscoveryHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.signals: list[_Signal] = []
        self.jsonld_blocks: list[tuple[str, str]] = []
        self.visible_parts: list[str] = []
        self._ignored_depth = 0
        self._jsonld_depth = 0
        self._jsonld_parts: list[str] = []
        self._jsonld_index = 0
        self._capture_tag: str | None = None
        self._capture_attrs: dict[str, str] = {}
        self._capture_parts: list[str] = []
        self._pending_label: tuple[str, str] | None = None
        self._title_depth = 0
        self._title_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {key.casefold(): (value or "") for key, value in attrs}
        tag = tag.casefold()

        if tag in {"style", "noscript"}:
            self._ignored_depth += 1
            return
        if tag == "script":
            content_type = attributes.get("type", "").casefold().split(";", 1)[0].strip()
            if content_type == "application/ld+json":
                self._jsonld_depth += 1
                if self._jsonld_depth == 1:
                    self._jsonld_parts = []
                    self._jsonld_index += 1
            else:
                self._ignored_depth += 1
            return
        if self._ignored_depth or self._jsonld_depth:
            return

        if tag == "meta":
            semantic_name = (
                attributes.get("property")
                or attributes.get("name")
                or attributes.get("itemprop")
            )
            content = attributes.get("content")
            if semantic_name and content:
                self.signals.append(
                    _Signal(
                        semantic_name,
                        content,
                        0.93,
                        Evidence(
                            kind="html_meta",
                            source_ref=semantic_name,
                            excerpt=content[:500],
                        ),
                    )
                )
            return

        if tag == "title":
            self._title_depth += 1
            if self._title_depth == 1:
                self._title_parts = []

        if tag in {
            "dt",
            "th",
            "label",
            "dd",
            "td",
            "data",
            "time",
            "input",
            "span",
            "div",
            "p",
        }:
            self._capture_tag = tag
            self._capture_attrs = attributes
            self._capture_parts = []

        semantic_name = attributes.get("itemprop")
        attribute_value = (
            attributes.get("content")
            or attributes.get("value")
            or attributes.get("datetime")
        )
        if semantic_name and attribute_value:
            self.signals.append(
                _Signal(
                    semantic_name,
                    attribute_value,
                    0.90,
                    Evidence(
                        kind="html_itemprop",
                        source_ref=semantic_name,
                        excerpt=attribute_value[:500],
                    ),
                )
            )

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in {"meta", "input"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "script":
            if self._jsonld_depth:
                self._jsonld_depth -= 1
                if self._jsonld_depth == 0:
                    self.jsonld_blocks.append(
                        (
                            f"script[{self._jsonld_index}]",
                            "".join(self._jsonld_parts),
                        )
                    )
                    self._jsonld_parts = []
            elif self._ignored_depth:
                self._ignored_depth -= 1
            return
        if tag in {"style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth or self._jsonld_depth:
            return

        if tag == "title" and self._title_depth:
            self._title_depth -= 1
            if self._title_depth == 0:
                value = _clean_text("".join(self._title_parts))
                if value:
                    self.signals.append(
                        _Signal(
                            "title",
                            value,
                            0.74,
                            Evidence(
                                kind="document_title",
                                source_ref="title",
                                excerpt=value[:500],
                            ),
                        )
                    )

        if self._capture_tag == tag:
            value = _clean_text("".join(self._capture_parts))
            attributes = self._capture_attrs
            semantic_name = (
                attributes.get("itemprop")
                or attributes.get("data-field")
                or attributes.get("data-label")
            )
            if semantic_name and value:
                self.signals.append(
                    _Signal(
                        semantic_name,
                        value,
                        0.88,
                        Evidence(
                            kind="html_semantic_element",
                            source_ref=semantic_name,
                            excerpt=value[:500],
                        ),
                    )
                )

            if tag in {"dt", "th", "label"} and value:
                self._pending_label = (tag, value)
            elif tag in {"dd", "td"} and value and self._pending_label:
                label_tag, label = self._pending_label
                valid_pair = (
                    (label_tag == "dt" and tag == "dd")
                    or (label_tag == "th" and tag == "td")
                )
                if valid_pair:
                    self.signals.append(
                        _Signal(
                            label,
                            value,
                            0.84,
                            Evidence(
                                kind="html_label_value",
                                source_ref=f"{label_tag}/{tag}:{label}",
                                excerpt=value[:500],
                            ),
                        )
                    )
                self._pending_label = None

            self._capture_tag = None
            self._capture_attrs = {}
            self._capture_parts = []

    def handle_data(self, data: str) -> None:
        if self._jsonld_depth:
            self._jsonld_parts.append(data)
            return
        if self._ignored_depth:
            return
        if self._title_depth:
            self._title_parts.append(data)
        if self._capture_tag:
            self._capture_parts.append(data)
        cleaned = _clean_text(data)
        if cleaned:
            self.visible_parts.append(cleaned)


def _walk_jsonld(value: Any, path: str = "$") -> Iterable[_Signal]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if isinstance(child, (str, int, float, bool)) and child not in ("", None):
                yield _Signal(
                    str(key),
                    child,
                    0.96,
                    Evidence(
                        kind="jsonld",
                        source_ref=child_path,
                        excerpt=str(child)[:500],
                    ),
                )
            elif (
                isinstance(child, list)
                and child
                and all(isinstance(item, (str, int, float, bool)) for item in child)
            ):
                yield _Signal(
                    str(key),
                    child,
                    0.94,
                    Evidence(
                        kind="jsonld",
                        source_ref=child_path,
                        excerpt=json.dumps(child, ensure_ascii=False)[:500],
                    ),
                )
            yield from _walk_jsonld(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_jsonld(child, f"{path}[{index}]")


def _field_terms(field: FieldSpec) -> dict[str, str]:
    terms = {field.key: _semantic_key(field.key)}
    terms.update({alias: _semantic_key(alias) for alias in field.aliases})
    return {raw: normalized for raw, normalized in terms.items() if normalized}


def _match_signal(
    signal: _Signal,
    field: FieldSpec,
) -> tuple[bool, str | None, float]:
    signal_key = _semantic_key(signal.semantic_name)
    if not signal_key:
        return False, None, 0.0
    field_key = _semantic_key(field.key)
    if signal_key == field_key:
        return True, field.key, signal.confidence
    for alias, alias_key in _field_terms(field).items():
        if alias == field.key:
            continue
        if signal_key == alias_key:
            return True, alias, max(0.0, signal.confidence - 0.02)
    return False, None, 0.0


def _text_signals(text: str) -> Iterable[_Signal]:
    for index, raw_line in enumerate(text.splitlines(), start=1):
        line = _clean_text(raw_line)
        if not line:
            continue
        match = _LABEL_VALUE_RE.match(line)
        if not match:
            continue
        label, value = (_clean_text(part) for part in match.groups())
        if label and value:
            yield _Signal(
                label,
                value,
                0.72,
                Evidence(
                    kind="text_label_value",
                    source_ref=f"line:{index}",
                    excerpt=line[:500],
                ),
            )


class AutoDiscoveryProvider:
    """Discover candidates from semantic structure without CSS/XPath selectors.

    Field keys and aliases express business semantics once in a domain schema. The
    provider searches portable web conventions (JSON-LD, meta/itemprop, label/value
    structures and a text fallback), then merges corroborating signals for the same
    field/value into one evidence-rich candidate.
    """

    name = "auto_discovery"

    def candidates(
        self,
        asset: RawAsset,
        fields: Sequence[FieldSpec],
    ) -> Sequence[Candidate]:
        signals: list[_Signal] = []
        text_sources: list[str] = []

        if asset.html:
            parser = _DiscoveryHTMLParser()
            parser.feed(asset.html)
            parser.close()
            signals.extend(parser.signals)
            text_sources.append("\n".join(parser.visible_parts))
            for source_ref, block in parser.jsonld_blocks:
                try:
                    payload = json.loads(block)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                signals.extend(_walk_jsonld(payload, path=source_ref))

        if asset.text:
            text_sources.append(asset.text)

        for text in text_sources:
            signals.extend(_text_signals(text))

        grouped: dict[
            tuple[str, str],
            list[tuple[_Signal, str, float]],
        ] = defaultdict(list)
        for signal in signals:
            if signal.value is None or signal.value == "":
                continue
            for field in fields:
                matched, matched_term, adjusted_confidence = _match_signal(
                    signal,
                    field,
                )
                if matched and matched_term:
                    grouped[
                        (field.key, _canonical_value(signal.value))
                    ].append((signal, matched_term, adjusted_confidence))

        result: list[Candidate] = []
        for (field_key, _), matches in grouped.items():
            ordered = sorted(
                matches,
                key=lambda item: (
                    -item[2],
                    item[0].evidence.kind,
                    item[0].evidence.source_ref,
                ),
            )
            best_signal, _, best_confidence = ordered[0]
            unique_evidence: list[Evidence] = []
            seen_evidence: set[tuple[str, str, str | None]] = set()
            matched_terms: list[str] = []
            signal_kinds: list[str] = []
            for signal, matched_term, _ in ordered:
                evidence_key = (
                    signal.evidence.kind,
                    signal.evidence.source_ref,
                    signal.evidence.excerpt,
                )
                if evidence_key not in seen_evidence:
                    seen_evidence.add(evidence_key)
                    unique_evidence.append(signal.evidence)
                if matched_term not in matched_terms:
                    matched_terms.append(matched_term)
                if signal.evidence.kind not in signal_kinds:
                    signal_kinds.append(signal.evidence.kind)

            corroboration_bonus = min(
                0.09,
                0.03 * max(0, len(signal_kinds) - 1),
            )
            confidence = min(0.99, best_confidence + corroboration_bonus)
            result.append(
                Candidate(
                    field_key=field_key,
                    value=best_signal.value,
                    confidence=confidence,
                    provider=self.name,
                    evidence=tuple(unique_evidence),
                    metadata={
                        "matched_terms": tuple(matched_terms),
                        "signal_kinds": tuple(signal_kinds),
                        "signal_count": len(unique_evidence),
                    },
                )
            )

        return tuple(
            sorted(
                result,
                key=lambda candidate: (
                    candidate.field_key,
                    -candidate.confidence,
                    candidate.candidate_id,
                ),
            )
        )
