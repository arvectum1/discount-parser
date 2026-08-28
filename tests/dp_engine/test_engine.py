from __future__ import annotations

from dataclasses import dataclass

import pytest

from arvectum_data.engine import (
    AttributeProvider,
    Candidate,
    Evidence,
    ExtractionEngine,
    FieldSpec,
    FieldStatus,
    RawAsset,
)


@dataclass
class StaticProvider:
    name: str
    produced: tuple[Candidate, ...]

    def candidates(self, asset, fields):
        return self.produced


class BrokenProvider:
    name = "broken"

    def candidates(self, asset, fields):
        raise RuntimeError("transport failed")


def candidate(field, value, confidence, provider):
    return Candidate(
        field_key=field,
        value=value,
        confidence=confidence,
        provider=provider,
        evidence=(Evidence(kind="test", source_ref="fixture"),),
    )


def test_high_confidence_candidate_is_auto_selected():
    engine = ExtractionEngine(
        [StaticProvider("primary", (candidate("title", "Example", 0.96, "primary"),))]
    )
    result = engine.extract(RawAsset("a1"), [FieldSpec("title", required=True)])

    assert result.decisions["title"].status is FieldStatus.AUTO_SELECTED
    assert result.values() == {"title": "Example"}
    assert not result.requires_confirmation


def test_close_candidates_require_human_confirmation():
    engine = ExtractionEngine(
        [
            StaticProvider("a", (candidate("price", 100, 0.91, "a"),)),
            StaticProvider("b", (candidate("price", 101, 0.88, "b"),)),
        ]
    )
    result = engine.extract(
        RawAsset("a1"),
        [FieldSpec("price", min_confidence=0.80, min_margin=0.05)],
    )

    decision = result.decisions["price"]
    assert decision.status is FieldStatus.NEEDS_CONFIRMATION
    assert decision.selected.value == 100
    assert result.requires_confirmation
    assert result.values() == {}
    assert result.values(include_unconfirmed=True) == {"price": 100}


def test_reviewer_can_only_choose_existing_candidate():
    engine = ExtractionEngine(
        [StaticProvider("a", (candidate("price", 100, 0.70, "a"),))]
    )
    result = engine.extract(RawAsset("a1"), [FieldSpec("price", min_confidence=0.80)])
    existing = result.decisions["price"].candidates[0]

    confirmed = engine.confirm(result, {"price": existing.candidate_id})
    assert confirmed.decisions["price"].status is FieldStatus.CONFIRMED
    assert confirmed.values() == {"price": 100}

    with pytest.raises(ValueError, match="manual values are not accepted"):
        engine.confirm(result, {"price": "invented-value"})


def test_reviewer_can_reject_but_not_overwrite_auto_selected_field():
    weak_engine = ExtractionEngine(
        [StaticProvider("a", (candidate("title", "Guess", 0.70, "a"),))]
    )
    weak = weak_engine.extract(RawAsset("a1"), [FieldSpec("title", min_confidence=0.80)])
    rejected = weak_engine.confirm(weak, {"title": None})
    assert rejected.decisions["title"].status is FieldStatus.REJECTED
    assert rejected.values() == {}

    strong_engine = ExtractionEngine(
        [StaticProvider("a", (candidate("title", "Certain", 0.99, "a"),))]
    )
    strong = strong_engine.extract(RawAsset("a2"), [FieldSpec("title")])
    with pytest.raises(ValueError, match="only review-required fields"):
        strong_engine.confirm(strong, {"title": None})


def test_required_unresolved_fields_are_explicit():
    result = ExtractionEngine([]).extract(
        RawAsset("a1"),
        [FieldSpec("required", required=True), FieldSpec("optional")],
    )

    assert result.decisions["required"].status is FieldStatus.UNRESOLVED
    assert result.unresolved_required_fields == ("required",)


def test_provider_failure_is_isolated():
    good = StaticProvider("good", (candidate("title", "Example", 0.99, "good"),))
    result = ExtractionEngine([BrokenProvider(), good]).extract(
        RawAsset("a1"), [FieldSpec("title")]
    )

    assert result.values() == {"title": "Example"}
    assert result.provider_errors == {"broken": "RuntimeError: transport failed"}


def test_attribute_provider_is_domain_neutral():
    provider = AttributeProvider({"canonical_name": "source_name"})
    result = ExtractionEngine([provider]).extract(
        RawAsset("a1", attributes={"source_name": "Widget"}),
        [FieldSpec("canonical_name")],
    )

    decision = result.decisions["canonical_name"]
    assert decision.status is FieldStatus.AUTO_SELECTED
    assert decision.selected.value == "Widget"
    assert decision.selected.evidence[0].source_ref == "source_name"


def test_duplicate_provider_and_field_names_are_rejected():
    empty = StaticProvider("same", ())
    with pytest.raises(ValueError, match="provider names"):
        ExtractionEngine([empty, empty])

    engine = ExtractionEngine([])
    with pytest.raises(ValueError, match="FieldSpec keys"):
        engine.extract(RawAsset("a1"), [FieldSpec("x"), FieldSpec("x")])
