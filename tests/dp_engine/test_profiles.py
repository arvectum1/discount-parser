from __future__ import annotations

import json
from pathlib import Path

import pytest

from arvectum_data import (
    AcquisitionResult,
    Candidate,
    ConfirmationLearner,
    Evidence,
    FieldSpec,
    InMemorySiteProfileStore,
    JsonSiteProfileStore,
    LearningPolicy,
    ProfileAwareProvider,
    ProfileSignalStats,
    RawAsset,
    URLExtractionPipeline,
    candidate_fingerprints,
    site_key_from_url,
)


def make_candidate(
    value,
    confidence,
    kind,
    source_ref,
    *,
    field="price",
    provider="test",
    terms=("price",),
):
    return Candidate(
        field_key=field,
        value=value,
        confidence=confidence,
        provider=provider,
        evidence=(Evidence(kind=kind, source_ref=source_ref),),
        metadata={"matched_terms": terms},
    )


class StaticProvider:
    name = "static"

    def __init__(self, candidates):
        self.produced = tuple(candidates)

    def candidates(self, asset, fields):
        return self.produced


class DynamicPriceProvider:
    name = "dynamic_price"

    def candidates(self, asset, fields):
        return (
            make_candidate(
                asset.attributes["json_price"],
                0.96,
                "jsonld",
                "script[7].offers[0].price",
                provider=self.name,
            ),
            make_candidate(
                asset.attributes["meta_price"],
                0.93,
                "html_meta",
                "price",
                provider=self.name,
            ),
        )


class WeakTextProvider:
    name = "weak_text"

    def candidates(self, asset, fields):
        return (
            make_candidate(
                asset.attributes["price"],
                0.70,
                "text_label_value",
                "line:143",
                provider=self.name,
                terms=("Цена",),
            ),
        )


class MutableAcquisition:
    def __init__(self, attributes):
        self.attributes = dict(attributes)

    def acquire(self, request):
        return AcquisitionResult(
            asset=RawAsset(
                request.resolved_asset_id,
                source_url=request.url,
                attributes=dict(self.attributes),
            ),
            attempts=(),
        )


def selected_by_kind(result, field_key, kind):
    return next(
        candidate
        for candidate in result.extraction.decisions[field_key].candidates
        if any(evidence.kind == kind for evidence in candidate.evidence)
    )


def test_site_key_is_exact_host_and_normalizes_default_ports():
    assert site_key_from_url("https://Shop.Example.com:443/item") == "shop.example.com"
    assert site_key_from_url("http://shop.example.com:8080/item") == "shop.example.com:8080"
    assert site_key_from_url("https://other.example.com/item") == "other.example.com"


def test_fingerprints_normalize_dynamic_indices_and_line_numbers():
    json_candidate = make_candidate(
        "199",
        0.96,
        "jsonld",
        "script[12].offers[3].price",
    )
    text_candidate = make_candidate(
        "199",
        0.70,
        "text_label_value",
        "line:928",
        terms=("Цена",),
    )

    json_fp = candidate_fingerprints(json_candidate)[0]
    text_fp = candidate_fingerprints(text_candidate)[0]

    assert json_fp.source_ref == "script[*].offers[*].price"
    assert text_fp.source_ref == "line:*"
    assert text_fp.semantic_terms == ("цена",)


def test_confirmation_learns_structure_not_candidate_values():
    selected = make_candidate("201", 0.93, "html_meta", "price")
    competing = make_candidate(
        "199",
        0.96,
        "jsonld",
        "script[1].offers.price",
    )
    decision = type("Decision", (), {"candidates": (selected, competing)})()
    result = type(
        "Result",
        (),
        {
            "asset": RawAsset("a1", source_url="https://shop.example.com/item"),
            "decisions": {"price": decision},
        },
    )()

    store = InMemorySiteProfileStore()
    events = ConfirmationLearner(store).learn(
        result,
        {"price": selected.candidate_id},
    )
    serialized = json.dumps(store.snapshot(), ensure_ascii=False)

    assert len(events) == 1
    assert "201" not in serialized
    assert "199" not in serialized
    assert events[0].selected_candidate_id == selected.candidate_id
    assert all("201" not in item for item in events[0].positive_fingerprints)


def test_profile_adjustment_reuses_structure_when_values_change():
    old_selected = make_candidate("201", 0.93, "html_meta", "price")
    old_competing = make_candidate(
        "199",
        0.96,
        "jsonld",
        "script[1].offers.price",
    )
    decision = type("Decision", (), {"candidates": (old_selected, old_competing)})()
    result = type(
        "Result",
        (),
        {
            "asset": RawAsset("a1", source_url="https://shop.example.com/item"),
            "decisions": {"price": decision},
        },
    )()
    store = InMemorySiteProfileStore()
    ConfirmationLearner(store).learn(
        result,
        {"price": old_selected.candidate_id},
    )

    new_json = make_candidate(
        "299",
        0.96,
        "jsonld",
        "script[99].offers.price",
    )
    new_meta = make_candidate("301", 0.93, "html_meta", "price")
    provider = ProfileAwareProvider(
        StaticProvider((new_json, new_meta)),
        store,
    )
    candidates = provider.candidates(
        RawAsset("a2", source_url="https://shop.example.com/next"),
        [FieldSpec("price")],
    )

    assert candidates[0].value == "299"
    assert candidates[0].confidence == pytest.approx(0.88)
    assert candidates[1].value == "301"
    assert candidates[1].confidence == pytest.approx(0.99)


def test_learning_does_not_cross_host_boundary():
    candidate = make_candidate("301", 0.93, "html_meta", "price")
    store = InMemorySiteProfileStore()
    store.record(
        "shop.example.com",
        "price",
        positive=candidate_fingerprints(candidate),
    )
    provider = ProfileAwareProvider(StaticProvider((candidate,)), store)

    other = provider.candidates(
        RawAsset("a2", source_url="https://other.example.com/item"),
        [FieldSpec("price")],
    )

    assert other[0].confidence == pytest.approx(0.93)
    assert "site_profile" not in other[0].metadata


def test_learning_policy_is_bounded():
    policy = LearningPolicy()
    positive = policy.adjustment(ProfileSignalStats(confirmations=100))
    negative = policy.adjustment(ProfileSignalStats(rejections=100))

    assert positive == pytest.approx(0.18)
    assert negative == pytest.approx(-0.24)


def test_reject_all_records_negative_evidence_only():
    a = make_candidate("199", 0.96, "jsonld", "script[1].price")
    b = make_candidate("201", 0.93, "html_meta", "price")
    decision = type("Decision", (), {"candidates": (a, b)})()
    result = type(
        "Result",
        (),
        {
            "asset": RawAsset("a1", source_url="https://shop.example.com/item"),
            "decisions": {"price": decision},
        },
    )()
    store = InMemorySiteProfileStore()

    events = ConfirmationLearner(store).learn(result, {"price": None})

    assert events[0].positive_fingerprints == ()
    for candidate in (a, b):
        stats = store.get_stats(
            "shop.example.com",
            "price",
            candidate_fingerprints(candidate)[0],
        )
        assert stats.confirmations == 0
        assert stats.rejections == 1


def test_overlapping_fingerprint_is_not_penalized_when_selected():
    selected = make_candidate("199", 0.90, "html_meta", "price")
    competing = make_candidate("201", 0.89, "html_meta", "price")
    decision = type("Decision", (), {"candidates": (selected, competing)})()
    result = type(
        "Result",
        (),
        {
            "asset": RawAsset("a1", source_url="https://shop.example.com/item"),
            "decisions": {"price": decision},
        },
    )()
    store = InMemorySiteProfileStore()

    ConfirmationLearner(store).learn(
        result,
        {"price": selected.candidate_id},
    )
    stats = store.get_stats(
        "shop.example.com",
        "price",
        candidate_fingerprints(selected)[0],
    )

    assert stats.confirmations == 1
    assert stats.rejections == 0


def test_json_profile_store_survives_reload(tmp_path: Path):
    candidate = make_candidate("201", 0.93, "html_meta", "price")
    path = tmp_path / "profiles.json"
    store = JsonSiteProfileStore(path)
    store.record(
        "shop.example.com",
        "price",
        positive=candidate_fingerprints(candidate),
    )

    reloaded = JsonSiteProfileStore(path)
    stats = reloaded.get_stats(
        "shop.example.com",
        "price",
        candidate_fingerprints(candidate)[0],
    )

    assert stats.confirmations == 1
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2


def test_pipeline_confirmation_removes_repeated_structured_ambiguity():
    acquisition = MutableAcquisition(
        {"json_price": "199", "meta_price": "201"}
    )
    pipeline = URLExtractionPipeline(
        acquisition=acquisition,
        providers=[DynamicPriceProvider()],
    )
    fields = [FieldSpec("price")]

    first = pipeline.extract_url("https://shop.example.com/item", fields)
    assert first.requires_confirmation
    meta = selected_by_kind(first, "price", "html_meta")
    confirmed = pipeline.confirm(first, {"price": meta.candidate_id})
    assert confirmed.values() == {"price": "201"}
    assert len(confirmed.learning_events) == 1

    acquisition.attributes = {"json_price": "299", "meta_price": "301"}
    second = pipeline.extract_url("https://shop.example.com/next", fields)

    assert second.ready
    assert second.values() == {"price": "301"}


def test_weak_signal_needs_repeated_confirmation_before_auto_selection():
    acquisition = MutableAcquisition({"price": "199"})
    pipeline = URLExtractionPipeline(
        acquisition=acquisition,
        providers=[WeakTextProvider()],
    )
    fields = [FieldSpec("price")]

    first = pipeline.extract_url("https://shop.example.com/a", fields)
    assert first.requires_confirmation
    first_candidate = first.extraction.decisions["price"].candidates[0]
    pipeline.confirm(first, {"price": first_candidate.candidate_id})

    acquisition.attributes = {"price": "200"}
    second = pipeline.extract_url("https://shop.example.com/b", fields)
    assert second.requires_confirmation
    assert second.extraction.decisions["price"].selected.confidence == pytest.approx(0.76)
    second_candidate = second.extraction.decisions["price"].candidates[0]
    pipeline.confirm(second, {"price": second_candidate.candidate_id})

    acquisition.attributes = {"price": "201"}
    third = pipeline.extract_url("https://shop.example.com/c", fields)
    assert third.ready
    assert third.values() == {"price": "201"}
    assert third.extraction.decisions["price"].selected.confidence == pytest.approx(0.82)


class FailingStore(InMemorySiteProfileStore):
    def record(self, site_key, field_key, *, positive=(), negative=()):
        raise OSError("profile disk unavailable")


def test_learning_failure_is_non_fatal_by_default():
    acquisition = MutableAcquisition(
        {"json_price": "199", "meta_price": "201"}
    )
    pipeline = URLExtractionPipeline(
        acquisition=acquisition,
        providers=[DynamicPriceProvider()],
        profile_store=FailingStore(),
    )
    result = pipeline.extract_url(
        "https://shop.example.com/item",
        [FieldSpec("price")],
    )
    meta = selected_by_kind(result, "price", "html_meta")

    confirmed = pipeline.confirm(result, {"price": meta.candidate_id})

    assert confirmed.values() == {"price": "201"}
    assert len(confirmed.learning_warnings) == 1
    assert confirmed.learning_warnings[0].startswith("profile_learning_failed:OSError:")


def test_strict_learning_surfaces_profile_store_failure():
    acquisition = MutableAcquisition(
        {"json_price": "199", "meta_price": "201"}
    )
    pipeline = URLExtractionPipeline(
        acquisition=acquisition,
        providers=[DynamicPriceProvider()],
        profile_store=FailingStore(),
        strict_learning=True,
    )
    result = pipeline.extract_url(
        "https://shop.example.com/item",
        [FieldSpec("price")],
    )
    meta = selected_by_kind(result, "price", "html_meta")

    with pytest.raises(OSError, match="profile disk unavailable"):
        pipeline.confirm(result, {"price": meta.candidate_id})


def test_learning_can_be_disabled():
    acquisition = MutableAcquisition(
        {"json_price": "199", "meta_price": "201"}
    )
    pipeline = URLExtractionPipeline(
        acquisition=acquisition,
        providers=[DynamicPriceProvider()],
        learning_enabled=False,
    )
    fields = [FieldSpec("price")]
    first = pipeline.extract_url("https://shop.example.com/item", fields)
    meta = selected_by_kind(first, "price", "html_meta")
    confirmed = pipeline.confirm(first, {"price": meta.candidate_id})

    acquisition.attributes = {"json_price": "299", "meta_price": "301"}
    second = pipeline.extract_url("https://shop.example.com/next", fields)

    assert confirmed.learning_events == ()
    assert second.requires_confirmation
