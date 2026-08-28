from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from arvectum_data import (
    AcquisitionAttempt,
    AcquisitionResult,
    Candidate,
    DurableReviewCoordinator,
    Evidence,
    ExtractionEngine,
    ExtractionJob,
    ExtractionResult,
    FieldDecision,
    FieldSpec,
    FieldStatus,
    InMemoryJobCheckpointStore,
    InMemoryResultStore,
    JobExecutor,
    JobItemStatus,
    JsonResultStore,
    RawAsset,
    ResultCodec,
    ResultConflictError,
    ResultDefinitionMismatchError,
    ResultIntegrityError,
    ResultRepository,
    ResultSerializationError,
    SQLiteResultStore,
    StoredResultStatus,
    URLExtractionPipeline,
    URLExtractionResult,
)
from arvectum_data.execution import JobCheckpoint, JobCheckpointItem
from arvectum_data.results.models import StoredResultRecord


def review_result(*, raw: bool = True) -> URLExtractionResult:
    asset = RawAsset(
        "asset-1",
        source_url="https://shop.example/item?token=secret",
        text="raw text" if raw else None,
        html="<html>raw</html>" if raw else None,
        attributes={"source": ("raw", 1)} if raw else {},
        metadata={"acquisition": {"rendered": False}},
    )
    field = FieldSpec("price", required=True, aliases=("Цена",))
    first = Candidate(
        "price",
        "199",
        0.90,
        "test",
        (Evidence("html_meta", "price", "199", {"bytes": b"x"}),),
        {"matched_terms": ("price",)},
    )
    second = Candidate(
        "price",
        "201",
        0.89,
        "test",
        (Evidence("jsonld", "offers[0].price", "201"),),
    )
    decision = FieldDecision(
        field,
        FieldStatus.NEEDS_CONFIRMATION,
        first,
        (first, second),
        "ambiguous",
    )
    extraction = ExtractionResult(asset, {"price": decision}, {"bad": "isolated"})
    acquisition = AcquisitionResult(
        asset,
        (AcquisitionAttempt("http", True, "ok", 200, "https://shop.example/item"),),
        ("warning",),
    )
    return URLExtractionResult(acquisition, extraction)


def ready_result() -> URLExtractionResult:
    source = review_result()
    decision = source.extraction.decisions["price"]
    selected = replace(
        decision,
        status=FieldStatus.AUTO_SELECTED,
        selected=decision.candidates[1],
    )
    return URLExtractionResult(
        source.acquisition,
        ExtractionResult(source.asset, {"price": selected}),
    )


def test_codec_round_trip_preserves_review_candidates_and_evidence():
    restored = ResultCodec().decode(ResultCodec().encode(review_result()))
    decision = restored.extraction.decisions["price"]

    assert restored.requires_confirmation
    assert [candidate.value for candidate in decision.candidates] == ["199", "201"]
    assert decision.candidates[0].evidence[0].metadata["bytes"] == b"x"
    assert restored.acquisition.attempts[0].status_code == 200


def test_default_codec_omits_raw_page_but_full_mode_can_preserve_it():
    minimal = ResultCodec().decode(ResultCodec().encode(review_result()))
    full_codec = ResultCodec(include_raw_content=True)
    full = full_codec.decode(full_codec.encode(review_result()))

    assert minimal.asset.text is None
    assert minimal.asset.html is None
    assert minimal.asset.attributes == {}
    assert full.asset.text == "raw text"
    assert full.asset.attributes["source"] == ("raw", 1)


def test_codec_rejects_unknown_value_types():
    source = review_result()
    decision = source.extraction.decisions["price"]
    bad = replace(decision.candidates[0], value=object())
    altered = replace(decision, selected=bad, candidates=(bad, decision.candidates[1]))
    result = URLExtractionResult(
        source.acquisition,
        ExtractionResult(source.asset, {"price": altered}),
    )

    with pytest.raises(ResultSerializationError, match="Unsupported"):
        ResultCodec().encode(result)


def test_record_integrity_detects_payload_tampering():
    store = InMemoryResultStore()
    repo = ResultRepository(store, clock=lambda: 10.0)
    record = repo.persist_initial(
        job_id="job",
        item_id="item",
        definition_hash="hash",
        result=review_result(),
    )
    payload = record.to_dict()
    payload["payload"]["learning_warnings"] = ["tampered"]

    with pytest.raises(ResultIntegrityError, match="hash mismatch"):
        StoredResultRecord.from_dict(payload)


def test_repository_initial_persist_is_idempotent_but_not_clobbering():
    store = InMemoryResultStore()
    repo = ResultRepository(store, clock=lambda: 10.0)
    first = repo.persist_initial(
        job_id="job",
        item_id="item",
        definition_hash="hash",
        result=review_result(),
    )
    same = repo.persist_initial(
        job_id="job",
        item_id="item",
        definition_hash="hash",
        result=review_result(),
    )

    assert first.revision == same.revision == 1
    with pytest.raises(ResultConflictError):
        repo.persist_initial(
            job_id="job",
            item_id="item",
            definition_hash="hash",
            result=ready_result(),
        )


def test_json_store_survives_reload_and_lists_pending_reviews(tmp_path: Path):
    repo = ResultRepository(JsonResultStore(tmp_path), clock=lambda: 20.0)
    repo.persist_initial(
        job_id="job",
        item_id="item",
        definition_hash="hash",
        result=review_result(),
    )

    reopened = ResultRepository(JsonResultStore(tmp_path))
    loaded = reopened.load_result("job", "item")
    assert loaded is not None
    record, result = loaded

    assert record.status is StoredResultStatus.REVIEW_REQUIRED
    assert result.requires_confirmation
    assert [item.item_id for item in reopened.pending_reviews(job_id="job")] == ["item"]


def test_sqlite_store_shares_state_and_enforces_revision(tmp_path: Path):
    path = tmp_path / "results.db"
    first_store = SQLiteResultStore(path)
    second_store = SQLiteResultStore(path)
    first_repo = ResultRepository(first_store, clock=lambda: 30.0)
    record = first_repo.persist_initial(
        job_id="job",
        item_id="item",
        definition_hash="hash",
        result=review_result(),
    )
    loaded = second_store.load("job", "item")

    assert loaded is not None and loaded.revision == 1
    with pytest.raises(ResultConflictError):
        second_store.update(record, expected_revision=0)
    first_store.close()
    second_store.close()


def test_review_confirmation_uses_persisted_candidates_without_reacquisition():
    store = InMemoryResultStore()
    repo = ResultRepository(store, clock=lambda: 1.0)
    record = repo.persist_initial(
        job_id="job",
        item_id="item",
        definition_hash="hash",
        result=review_result(),
    )
    coordinator = DurableReviewCoordinator(
        store,
        pipeline=URLExtractionPipeline(
            extraction=ExtractionEngine(()),
            learning_enabled=False,
        ),
        repository=repo,
        clock=lambda: 2.0,
    )
    _, persisted = coordinator.get("job", "item")
    chosen = persisted.extraction.decisions["price"].candidates[1]

    updated = coordinator.confirm(
        "job",
        "item",
        {"price": chosen.candidate_id},
        expected_revision=record.revision,
    )

    assert updated.record.status is StoredResultStatus.READY
    assert updated.result.values() == {"price": "201"}
    assert coordinator.pending(job_id="job") == ()


def test_review_checkpoint_sync_turns_review_item_into_success():
    store = InMemoryResultStore()
    repo = ResultRepository(store, clock=lambda: 1.0)
    repo.persist_initial(
        job_id="job",
        item_id="item",
        definition_hash="hash",
        result=review_result(),
    )
    checkpoints = InMemoryJobCheckpointStore()
    checkpoints.save(
        JobCheckpoint(
            "job",
            "hash",
            {"item": JobCheckpointItem(JobItemStatus.REVIEW_REQUIRED, 1, ("price",))},
        )
    )
    coordinator = DurableReviewCoordinator(
        store,
        pipeline=URLExtractionPipeline(
            extraction=ExtractionEngine(()),
            learning_enabled=False,
        ),
        checkpoint_store=checkpoints,
        repository=repo,
        clock=lambda: 2.0,
    )
    _, persisted = coordinator.get("job", "item")
    candidate = persisted.extraction.decisions["price"].candidates[0]

    update = coordinator.confirm("job", "item", {"price": candidate.candidate_id})

    assert update.checkpoint_synced
    checkpoint = checkpoints.load("job")
    assert checkpoint is not None
    assert checkpoint.items["item"].status is JobItemStatus.SUCCEEDED


def test_review_definition_mismatch_is_rejected_before_confirmation():
    store = InMemoryResultStore()
    ResultRepository(store).persist_initial(
        job_id="job",
        item_id="item",
        definition_hash="hash-a",
        result=review_result(),
    )
    coordinator = DurableReviewCoordinator(store)

    with pytest.raises(ResultDefinitionMismatchError):
        coordinator.get("job", "item", expected_definition_hash="hash-b")


class StaticPipeline:
    def __init__(self, result: URLExtractionResult):
        self.result = result
        self.calls = 0

    def extract(self, request, fields):
        self.calls += 1
        return self.result


def test_executor_persists_terminal_result_and_resume_rehydrates_payload():
    pipeline = StaticPipeline(ready_result())
    results = InMemoryResultStore()
    checkpoints = InMemoryJobCheckpointStore()
    executor = JobExecutor(
        pipeline,
        checkpoint_store=checkpoints,
        result_store=results,
        sleeper=lambda _: None,
        clock=lambda: 1.0,
    )
    job = ExtractionJob.from_urls(
        "job",
        ["https://shop.example/item"],
        [FieldSpec("price")],
    )

    first = executor.run(job)
    second = executor.run(job)

    assert pipeline.calls == 1
    assert first.items[0].status is JobItemStatus.SUCCEEDED
    assert second.items[0].resumed
    assert second.items[0].result is not None
    assert second.items[0].result.values() == {"price": "201"}


class FailTerminalCheckpointOnce(InMemoryJobCheckpointStore):
    def __init__(self):
        super().__init__()
        self.failed = False

    def save(self, checkpoint):
        state = next(iter(checkpoint.items.values()))
        if not self.failed and state.status is JobItemStatus.SUCCEEDED:
            self.failed = True
            raise OSError("checkpoint unavailable")
        return super().save(checkpoint)


def test_crash_window_recovers_durable_result_without_refetch():
    pipeline = StaticPipeline(ready_result())
    results = InMemoryResultStore()
    checkpoints = FailTerminalCheckpointOnce()
    executor = JobExecutor(
        pipeline,
        checkpoint_store=checkpoints,
        result_store=results,
        sleeper=lambda _: None,
        clock=lambda: 1.0,
    )
    job = ExtractionJob.from_urls(
        "job",
        ["https://shop.example/item"],
        [FieldSpec("price")],
    )

    with pytest.raises(OSError, match="checkpoint unavailable"):
        executor.run(job)
    assert pipeline.calls == 1

    resumed = executor.run(job)

    assert pipeline.calls == 1
    assert resumed.items[0].resumed
    assert resumed.items[0].status is JobItemStatus.SUCCEEDED


def test_resume_false_clears_prior_durable_results():
    pipeline = StaticPipeline(ready_result())
    results = InMemoryResultStore()
    executor = JobExecutor(
        pipeline,
        checkpoint_store=InMemoryJobCheckpointStore(),
        result_store=results,
        sleeper=lambda _: None,
        clock=lambda: 1.0,
    )
    job = ExtractionJob.from_urls(
        "job",
        ["https://shop.example/item"],
        [FieldSpec("price")],
    )

    executor.run(job)
    assert results.list(job_id="job")
    executor.run(job, resume=False)

    assert pipeline.calls == 2
    assert len(results.list(job_id="job")) == 1


def test_review_update_is_optimistically_revision_guarded():
    store = InMemoryResultStore()
    repo = ResultRepository(store, clock=lambda: 1.0)
    record = repo.persist_initial(
        job_id="job",
        item_id="item",
        definition_hash="hash",
        result=review_result(),
    )
    coordinator = DurableReviewCoordinator(
        store,
        pipeline=URLExtractionPipeline(
            extraction=ExtractionEngine(()),
            learning_enabled=False,
        ),
        repository=repo,
    )
    candidate = coordinator.get("job", "item")[1].extraction.decisions["price"].candidates[0]

    with pytest.raises(ResultConflictError):
        coordinator.confirm(
            "job",
            "item",
            {"price": candidate.candidate_id},
            expected_revision=record.revision + 1,
        )
