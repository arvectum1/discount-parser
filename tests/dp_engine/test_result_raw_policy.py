from __future__ import annotations

from arvectum_data import (
    AcquisitionResult,
    Candidate,
    DurableReviewCoordinator,
    Evidence,
    ExtractionEngine,
    ExtractionResult,
    FieldDecision,
    FieldSpec,
    FieldStatus,
    InMemoryResultStore,
    RawAsset,
    ResultCodec,
    ResultRepository,
    URLExtractionPipeline,
    URLExtractionResult,
)


def test_review_update_preserves_original_raw_content_policy():
    asset = RawAsset(
        "asset-1",
        source_url="https://shop.example/item",
        text="raw text",
        html="<html>raw</html>",
        attributes={"source": ("raw", 1)},
    )
    field = FieldSpec("price")
    candidate = Candidate(
        "price",
        "199",
        0.75,
        "test",
        (Evidence("text_label_value", "line:1"),),
    )
    result = URLExtractionResult(
        AcquisitionResult(asset, ()),
        ExtractionResult(
            asset,
            {
                "price": FieldDecision(
                    field,
                    FieldStatus.NEEDS_CONFIRMATION,
                    candidate,
                    (candidate,),
                )
            },
        ),
    )

    store = InMemoryResultStore()
    full_repo = ResultRepository(
        store,
        codec=ResultCodec(include_raw_content=True),
        clock=lambda: 1.0,
    )
    full_repo.persist_initial(
        job_id="job-full",
        item_id="item",
        definition_hash="hash",
        result=result,
    )

    coordinator = DurableReviewCoordinator(
        store,
        pipeline=URLExtractionPipeline(
            extraction=ExtractionEngine(()),
            learning_enabled=False,
        ),
        clock=lambda: 2.0,
    )
    record, persisted = coordinator.get("job-full", "item")
    persisted_candidate = persisted.extraction.decisions["price"].candidates[0]

    coordinator.confirm(
        "job-full",
        "item",
        {"price": persisted_candidate.candidate_id},
        expected_revision=record.revision,
    )
    updated_record, updated_result = coordinator.get("job-full", "item")

    assert updated_record.payload["raw_content_persisted"] is True
    assert updated_result.asset.html == "<html>raw</html>"
    assert updated_result.asset.attributes["source"] == ("raw", 1)
