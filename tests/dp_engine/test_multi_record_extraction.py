from __future__ import annotations

from dataclasses import replace

import pytest

from arvectum_data import (
    AttributeProvider,
    AttributeRecordProvider,
    AutoDiscoveryProvider,
    Candidate,
    Evidence,
    FieldSpec,
    JSONLDRecordProvider,
    MultiRecordExtractionEngine,
    RawAsset,
    RecordBoundary,
    RecordBoundaryStatus,
    RecordProviderResult,
    RecordStatus,
    make_record_id,
)


FIELDS = (
    FieldSpec("title", required=True, aliases=("name",)),
    FieldSpec("promo_code", required=True, aliases=("code", "promocode")),
)


def _structured_engine(*, min_boundary_confidence: float = 0.80):
    return MultiRecordExtractionEngine(
        (AttributeRecordProvider(),),
        (AttributeProvider({"title": "title", "promo_code": "code"}),),
        min_boundary_confidence=min_boundary_confidence,
    )


def test_structured_records_are_resolved_independently() -> None:
    asset = RawAsset(
        asset_id="page-1",
        source_url="https://example.test/offers",
        attributes={
            "records": [
                {"title": "First offer", "code": "FIRST10"},
                {"title": "Second offer", "code": "SECOND20"},
            ]
        },
    )

    result = _structured_engine().extract(asset, FIELDS)

    assert len(result.records) == 2
    assert [record.status for record in result.records] == [
        RecordStatus.READY,
        RecordStatus.READY,
    ]
    values = list(result.values().values())
    assert values == [
        {"title": "First offer", "promo_code": "FIRST10"},
        {"title": "Second offer", "promo_code": "SECOND20"},
    ]
    assert all(len(record.extraction.decisions["promo_code"].candidates) == 1 for record in result.records)


def test_jsonld_item_list_produces_two_record_scoped_extractions() -> None:
    asset = RawAsset(
        asset_id="jsonld-page",
        source_url="https://example.test/coupons",
        html="""
        <html><body>
        <script type="application/ld+json">
        {
          "@type": "ItemList",
          "itemListElement": [
            {"@type": "Offer", "name": "Alpha", "code": "ALPHA10"},
            {"@type": "Offer", "name": "Beta", "code": "BETA20"}
          ]
        }
        </script>
        </body></html>
        """,
    )
    engine = MultiRecordExtractionEngine(
        (JSONLDRecordProvider(),),
        (AutoDiscoveryProvider(),),
    )

    result = engine.extract(asset, FIELDS)

    assert len(result.records) == 2
    assert result.record_provider_errors == {}
    assert [record.boundary.metadata["jsonld_type"] for record in result.records] == [
        "Offer",
        "Offer",
    ]
    assert list(result.values().values()) == [
        {"title": "Alpha", "promo_code": "ALPHA10"},
        {"title": "Beta", "promo_code": "BETA20"},
    ]
    assert all(
        evidence.kind == "jsonld_record_boundary"
        for record in result.records
        for evidence in record.boundary.evidence
    )


def test_jsonld_parent_container_is_not_mistaken_for_a_record() -> None:
    asset = RawAsset(
        asset_id="jsonld-parent",
        html="""
        <script type="application/ld+json">
        {"@type":"ItemList","itemListElement":[
          {"@type":"Offer","name":"A","code":"A1"},
          {"@type":"Offer","name":"B","code":"B2"}
        ]}
        </script>
        """,
    )
    provider = JSONLDRecordProvider()

    discovered = provider.records(asset, FIELDS)

    assert len(discovered.records) == 2
    assert all("itemListElement" in record.source_ref for record in discovered.records)


def test_malformed_jsonld_isolated_and_reported_as_warning() -> None:
    asset = RawAsset(
        asset_id="jsonld-warning",
        html="""
        <script type="application/ld+json">{not valid json</script>
        <script type="application/ld+json">{"name":"Valid","code":"OK10"}</script>
        """,
    )
    provider = JSONLDRecordProvider()

    discovered = provider.records(asset, FIELDS)

    assert len(discovered.records) == 1
    assert discovered.warnings == ("malformed_jsonld:script[1]",)


def test_jsonld_provider_is_bounded_and_reports_truncation() -> None:
    asset = RawAsset(
        asset_id="jsonld-bounded",
        html="""
        <script type="application/ld+json">
        [{"name":"A","code":"A"},{"name":"B","code":"B"},{"name":"C","code":"C"}]
        </script>
        """,
    )
    provider = JSONLDRecordProvider(max_records=2)

    discovered = provider.records(asset, FIELDS)

    assert len(discovered.records) == 2
    assert discovered.warnings == ("max_records:2",)


def test_attribute_provider_is_bounded_and_preserves_order() -> None:
    asset = RawAsset(
        asset_id="attrs-bounded",
        attributes={"records": [{"title": "A"}, {"title": "B"}, {"title": "C"}]},
    )
    provider = AttributeRecordProvider(max_records=2)

    discovered = provider.records(asset, FIELDS)

    assert [record.asset.attributes["title"] for record in discovered.records] == ["A", "B"]
    assert [record.ordinal for record in discovered.records] == [0, 1]
    assert discovered.warnings == ("max_records:2",)


def test_low_confidence_boundary_requires_explicit_review() -> None:
    class LowConfidenceProvider:
        name = "low"

        def records(self, asset, fields):
            source_ref = "record[0]"
            child = RawAsset(
                asset_id="low-child",
                source_url=asset.source_url,
                attributes={"title": "Low", "code": "LOW10"},
            )
            return RecordProviderResult(
                records=(
                    RecordBoundary(
                        record_id=make_record_id(asset.asset_id, self.name, source_ref),
                        asset=child,
                        provider=self.name,
                        source_ref=source_ref,
                        ordinal=0,
                        confidence=0.40,
                        evidence=(Evidence("test_boundary", source_ref),),
                    ),
                )
            )

    engine = MultiRecordExtractionEngine(
        (LowConfidenceProvider(),),
        (AttributeProvider({"title": "title", "promo_code": "code"}),),
    )
    result = engine.extract(RawAsset("parent"), FIELDS)
    record_id = result.records[0].record_id

    assert result.records[0].boundary_status is RecordBoundaryStatus.NEEDS_CONFIRMATION
    assert result.records[0].status is RecordStatus.NEEDS_CONFIRMATION
    assert result.values() == {}
    assert result.values(include_unconfirmed=True)[record_id]["promo_code"] == "LOW10"

    accepted = engine.confirm_boundary(result, record_id, accept=True)
    assert accepted.record(record_id).boundary_status is RecordBoundaryStatus.CONFIRMED
    assert accepted.record(record_id).status is RecordStatus.READY
    assert accepted.values()[record_id]["promo_code"] == "LOW10"


def test_rejected_boundary_keeps_evidence_but_exports_no_values() -> None:
    class LowConfidenceProvider:
        name = "low"

        def records(self, asset, fields):
            child = RawAsset(asset_id="child", attributes={"title": "Offer", "code": "X"})
            return RecordProviderResult(
                records=(
                    RecordBoundary(
                        record_id="rec-low",
                        asset=child,
                        provider=self.name,
                        source_ref="r[0]",
                        ordinal=0,
                        confidence=0.20,
                        evidence=(Evidence("boundary", "r[0]", excerpt="proposed"),),
                    ),
                )
            )

    engine = MultiRecordExtractionEngine(
        (LowConfidenceProvider(),),
        (AttributeProvider({"title": "title", "promo_code": "code"}),),
    )
    initial = engine.extract(RawAsset("parent"), FIELDS)
    rejected = engine.confirm_boundary(initial, "rec-low", accept=False)
    record = rejected.record("rec-low")

    assert record.status is RecordStatus.REJECTED
    assert record.boundary.evidence[0].excerpt == "proposed"
    assert record.extraction.decisions["promo_code"].selected is not None
    assert rejected.values() == {}
    with pytest.raises(ValueError, match="rejected"):
        engine.confirm_fields(rejected, "rec-low", {})


def test_auto_selected_boundary_cannot_be_re_reviewed() -> None:
    result = _structured_engine().extract(
        RawAsset("parent", attributes={"records": [{"title": "A", "code": "A"}]}),
        FIELDS,
    )
    record_id = result.records[0].record_id

    with pytest.raises(ValueError, match="only review-required boundaries"):
        _structured_engine().confirm_boundary(result, record_id, accept=True)


def test_missing_required_field_is_incomplete_per_record() -> None:
    result = _structured_engine().extract(
        RawAsset("parent", attributes={"records": [{"title": "A"}, {"title": "B", "code": "B"}]}),
        FIELDS,
    )

    assert result.records[0].status is RecordStatus.INCOMPLETE
    assert result.records[0].unresolved_required_fields == ("promo_code",)
    assert result.records[1].status is RecordStatus.READY
    assert result.incomplete_record_ids == (result.records[0].record_id,)
    assert result.ready_record_ids == (result.records[1].record_id,)


def test_field_review_is_scoped_to_one_record() -> None:
    class AmbiguousProvider:
        name = "ambiguous"

        def candidates(self, asset, fields):
            record_name = asset.attributes["record_name"]
            result = [
                Candidate(
                    field_key="title",
                    value=record_name,
                    confidence=0.99,
                    provider=self.name,
                )
            ]
            if record_name == "first":
                result.extend(
                    [
                        Candidate("promo_code", "A", 0.90, self.name),
                        Candidate("promo_code", "B", 0.85, self.name),
                    ]
                )
            else:
                result.append(Candidate("promo_code", "C", 0.99, self.name))
            return result

    class TwoRecords:
        name = "two"

        def records(self, asset, fields):
            boundaries = []
            for index, record_name in enumerate(("first", "second")):
                child = RawAsset(f"child-{index}", attributes={"record_name": record_name})
                source_ref = f"record[{index}]"
                boundaries.append(
                    RecordBoundary(
                        record_id=make_record_id(asset.asset_id, self.name, source_ref),
                        asset=child,
                        provider=self.name,
                        source_ref=source_ref,
                        ordinal=index,
                        confidence=0.99,
                    )
                )
            return RecordProviderResult(records=tuple(boundaries))

    engine = MultiRecordExtractionEngine((TwoRecords(),), (AmbiguousProvider(),))
    initial = engine.extract(RawAsset("parent"), FIELDS)
    first, second = initial.records

    assert first.status is RecordStatus.NEEDS_CONFIRMATION
    assert second.status is RecordStatus.READY
    assert initial.review_record_ids == (first.record_id,)

    chosen = first.extraction.decisions["promo_code"].candidates[1].candidate_id
    reviewed = engine.confirm_fields(initial, first.record_id, {"promo_code": chosen})

    assert reviewed.record(first.record_id).status is RecordStatus.READY
    assert reviewed.record(first.record_id).values()["promo_code"] == "B"
    assert reviewed.record(second.record_id).values()["promo_code"] == "C"


def test_candidate_from_other_record_cannot_be_used_for_review() -> None:
    class Provider:
        name = "provider"

        def candidates(self, asset, fields):
            code = asset.attributes["code"]
            if code == "one":
                return (
                    Candidate("title", "One", 0.99, self.name),
                    Candidate("promo_code", "ONE-A", 0.90, self.name),
                    Candidate("promo_code", "ONE-B", 0.85, self.name),
                )
            return (
                Candidate("title", "Two", 0.99, self.name),
                Candidate("promo_code", "TWO", 0.99, self.name),
            )

    class Records:
        name = "records"

        def records(self, asset, fields):
            return RecordProviderResult(
                records=tuple(
                    RecordBoundary(
                        record_id=f"record-{index}",
                        asset=RawAsset(f"child-{index}", attributes={"code": code}),
                        provider=self.name,
                        source_ref=f"r[{index}]",
                        ordinal=index,
                        confidence=0.99,
                    )
                    for index, code in enumerate(("one", "two"))
                )
            )

    engine = MultiRecordExtractionEngine((Records(),), (Provider(),))
    initial = engine.extract(RawAsset("parent"), FIELDS)
    other_id = initial.record("record-1").extraction.decisions["promo_code"].selected.candidate_id

    with pytest.raises(ValueError, match="Unknown candidate_id"):
        engine.confirm_fields(initial, "record-0", {"promo_code": other_id})


def test_record_provider_failure_is_isolated() -> None:
    class Broken:
        name = "broken"

        def records(self, asset, fields):
            raise RuntimeError("boom")

    engine = MultiRecordExtractionEngine(
        (Broken(), AttributeRecordProvider()),
        (AttributeProvider({"title": "title", "promo_code": "code"}),),
    )
    result = engine.extract(
        RawAsset("parent", attributes={"records": [{"title": "A", "code": "A"}]}),
        FIELDS,
    )

    assert len(result.records) == 1
    assert result.records[0].status is RecordStatus.READY
    assert result.record_provider_errors == {"broken": "RuntimeError: boom"}


def test_provider_cannot_spoof_another_provider_name() -> None:
    class Spoofed:
        name = "actual"

        def records(self, asset, fields):
            return RecordProviderResult(
                records=(
                    RecordBoundary(
                        record_id="spoof",
                        asset=RawAsset("child"),
                        provider="other",
                        source_ref="r[0]",
                        ordinal=0,
                        confidence=1.0,
                    ),
                )
            )

    result = MultiRecordExtractionEngine((Spoofed(),), ()).extract(RawAsset("parent"), FIELDS)

    assert result.records == ()
    assert "does not match producer" in result.record_provider_errors["actual"]


def test_duplicate_record_ids_from_provider_are_isolated() -> None:
    class Duplicate:
        name = "duplicate"

        def records(self, asset, fields):
            return RecordProviderResult(
                records=(
                    RecordBoundary("same", RawAsset("a"), self.name, "r[0]", 0, 1.0),
                    RecordBoundary("same", RawAsset("b"), self.name, "r[1]", 1, 1.0),
                )
            )

    result = MultiRecordExtractionEngine((Duplicate(),), ()).extract(RawAsset("parent"), FIELDS)

    assert result.records == ()
    assert "Duplicate record_id" in result.record_provider_errors["duplicate"]


def test_record_provider_names_must_be_unique() -> None:
    first = AttributeRecordProvider(name="same")
    second = AttributeRecordProvider(name="same")

    with pytest.raises(ValueError, match="Record provider names must be unique"):
        MultiRecordExtractionEngine((first, second), ())


def test_duplicate_field_keys_rejected_even_when_no_records_exist() -> None:
    engine = MultiRecordExtractionEngine((), ())

    with pytest.raises(ValueError, match="FieldSpec keys must be unique"):
        engine.extract(RawAsset("parent"), (FieldSpec("x"), FieldSpec("x")))


def test_empty_record_set_is_valid() -> None:
    result = MultiRecordExtractionEngine((JSONLDRecordProvider(),), (AutoDiscoveryProvider(),)).extract(
        RawAsset("empty", html="<html><body>No records</body></html>"),
        FIELDS,
    )

    assert result.records == ()
    assert result.values() == {}
    assert not result.requires_confirmation


def test_record_ids_depend_on_structure_not_business_values() -> None:
    first = make_record_id("page", "provider", "items[0]")
    second = make_record_id("page", "provider", "items[0]")
    moved = make_record_id("page", "provider", "items[1]")

    assert first == second
    assert first != moved


def test_record_order_is_deterministic_across_providers() -> None:
    class Provider:
        def __init__(self, name, ordinal):
            self.name = name
            self.ordinal = ordinal

        def records(self, asset, fields):
            source_ref = f"{self.name}[0]"
            return RecordProviderResult(
                records=(
                    RecordBoundary(
                        record_id=make_record_id(asset.asset_id, self.name, source_ref),
                        asset=RawAsset(f"{self.name}-child"),
                        provider=self.name,
                        source_ref=source_ref,
                        ordinal=self.ordinal,
                        confidence=1.0,
                    ),
                )
            )

    result = MultiRecordExtractionEngine((Provider("z", 2), Provider("a", 0), Provider("b", 0)), ()).extract(
        RawAsset("parent"),
        (),
    )

    assert [record.boundary.provider for record in result.records] == ["a", "b", "z"]


def test_attribute_record_input_must_be_sequence_of_mappings() -> None:
    provider = AttributeRecordProvider()

    with pytest.raises(ValueError, match="must be a sequence"):
        provider.records(RawAsset("bad", attributes={"records": {"title": "A"}}), FIELDS)
    with pytest.raises(ValueError, match="must be a mapping"):
        provider.records(RawAsset("bad", attributes={"records": ["not-a-record"]}), FIELDS)


def test_record_result_rejects_mismatched_boundary_and_extraction_assets() -> None:
    # Contract invariant is covered indirectly by constructing a valid result first,
    # then proving the dataclass refuses an extraction from another record asset.
    initial = _structured_engine().extract(
        RawAsset("parent", attributes={"records": [{"title": "A", "code": "A"}]}),
        FIELDS,
    )
    record = initial.records[0]

    with pytest.raises(ValueError, match="must match"):
        replace(record, extraction=replace(record.extraction, asset=RawAsset("other")))
