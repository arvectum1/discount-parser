from __future__ import annotations

from types import SimpleNamespace

import pytest

from arvectum_data.acquisition import AcquisitionError, RenderMode
from arvectum_data.engine import FieldSpec, FieldStatus
from arvectum_data.execution import (
    ExtractionJob,
    InMemoryJobCheckpointStore,
    JobCheckpoint,
    JobCheckpointItem,
    JobCheckpointMismatchError,
    JobExecutor,
    JobItem,
    JobItemStatus,
    JobStatus,
    JsonJobCheckpointStore,
    RetryPolicy,
)


class Decision:
    def __init__(self, status):
        self.status = status


class FakeResult:
    def __init__(self, kind="ready"):
        self.requires_confirmation = kind == "review"
        self.unresolved_required_fields = ("price",) if kind == "incomplete" else ()
        self.extraction = SimpleNamespace(
            decisions={
                "price": Decision(FieldStatus.NEEDS_CONFIRMATION)
            }
            if kind == "review"
            else {}
        )
        self.kind = kind


class FakePipeline:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def extract(self, request, fields):
        self.calls.append((request, tuple(fields)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResult(outcome)


def clock_counter():
    state = {"value": 0.0}

    def clock():
        state["value"] += 1.0
        return state["value"]

    return clock


def make_job(job_id="job", urls=("https://a.test",)):
    return ExtractionJob.from_urls(
        job_id,
        urls,
        [FieldSpec("price", required=True)],
    )


def test_job_item_derives_deterministic_id_and_preserves_transport_controls():
    a = JobItem(
        "https://a.test/item",
        headers={"X-Test": "1"},
        timeout_s=3,
        max_bytes=1234,
        render_mode=RenderMode.ALWAYS,
    )
    b = JobItem("https://a.test/item")
    assert a.item_id == b.item_id
    request = a.acquisition_request()
    assert request.headers == {"X-Test": "1"}
    assert request.timeout_s == 3
    assert request.max_bytes == 1234
    assert request.render_mode is RenderMode.ALWAYS


def test_job_rejects_duplicate_derived_item_ids():
    with pytest.raises(ValueError, match="item_id"):
        ExtractionJob.from_urls(
            "job",
            ["https://a.test", "https://a.test"],
            [FieldSpec("price")],
        )


def test_definition_hash_changes_when_semantic_or_transport_input_changes():
    base = ExtractionJob(
        "job",
        (JobItem("https://a.test", headers={"X": "1"}),),
        (FieldSpec("price"),),
    )
    changed_header = ExtractionJob(
        "job",
        (JobItem("https://a.test", headers={"X": "2"}),),
        (FieldSpec("price"),),
    )
    changed_field = ExtractionJob(
        "job",
        (JobItem("https://a.test", headers={"X": "1"}),),
        (FieldSpec("price", min_confidence=0.9),),
    )
    assert base.definition_hash != changed_header.definition_hash
    assert base.definition_hash != changed_field.definition_hash


def test_retryable_acquisition_failure_retries_then_succeeds():
    sleeps = []
    pipeline = FakePipeline([AcquisitionError("temporary"), "ready"])
    executor = JobExecutor(
        pipeline,
        retry_policy=RetryPolicy(max_attempts=3, base_delay_s=1),
        checkpoint_store=InMemoryJobCheckpointStore(),
        sleeper=sleeps.append,
        clock=clock_counter(),
    )
    result = executor.run(make_job())
    assert result.status is JobStatus.SUCCEEDED
    assert result.items[0].attempt_count == 2
    assert [attempt.success for attempt in result.items[0].attempts] == [False, True]
    assert sleeps == [1.0]


def test_backoff_is_exponential_and_capped():
    policy = RetryPolicy(base_delay_s=2, multiplier=3, max_delay_s=10)
    assert policy.delay_after(1) == 2
    assert policy.delay_after(2) == 6
    assert policy.delay_after(3) == 10


def test_unexpected_runtime_error_is_not_retried():
    pipeline = FakePipeline([RuntimeError("bug")])
    executor = JobExecutor(
        pipeline,
        checkpoint_store=InMemoryJobCheckpointStore(),
        sleeper=lambda _: pytest.fail("unexpected sleep"),
    )
    result = executor.run(make_job())
    assert result.items[0].status is JobItemStatus.FAILED
    assert result.items[0].attempt_count == 1
    assert result.items[0].error.error_type == "RuntimeError"
    assert len(pipeline.calls) == 1


def test_retryable_error_stops_at_retry_budget():
    pipeline = FakePipeline([
        AcquisitionError("1"),
        AcquisitionError("2"),
        AcquisitionError("3"),
    ])
    executor = JobExecutor(
        pipeline,
        retry_policy=RetryPolicy(max_attempts=3, base_delay_s=0),
        checkpoint_store=InMemoryJobCheckpointStore(),
        sleeper=lambda _: None,
    )
    result = executor.run(make_job())
    assert result.items[0].status is JobItemStatus.FAILED
    assert result.items[0].attempt_count == 3
    assert len(pipeline.calls) == 3


def test_item_failure_is_isolated_from_later_items():
    pipeline = FakePipeline([RuntimeError("bad item"), "ready"])
    executor = JobExecutor(
        pipeline,
        checkpoint_store=InMemoryJobCheckpointStore(),
    )
    result = executor.run(make_job(urls=("https://a.test", "https://b.test")))
    assert [item.status for item in result.items] == [
        JobItemStatus.FAILED,
        JobItemStatus.SUCCEEDED,
    ]
    assert result.status is JobStatus.COMPLETED_WITH_FAILURES


def test_review_and_incomplete_are_not_execution_failures_or_retried():
    pipeline = FakePipeline(["review", "incomplete"])
    executor = JobExecutor(
        pipeline,
        checkpoint_store=InMemoryJobCheckpointStore(),
    )
    result = executor.run(make_job(urls=("https://a.test", "https://b.test")))
    assert result.items[0].status is JobItemStatus.REVIEW_REQUIRED
    assert result.items[0].review_fields == ("price",)
    assert result.items[1].status is JobItemStatus.INCOMPLETE
    assert result.items[1].unresolved_required_fields == ("price",)
    assert result.status is JobStatus.NEEDS_ATTENTION
    assert len(pipeline.calls) == 2


def test_chunked_run_resumes_without_reprocessing_completed_items():
    store = InMemoryJobCheckpointStore()
    pipeline = FakePipeline(["ready", "ready", "ready"])
    executor = JobExecutor(pipeline, checkpoint_store=store)
    job = make_job(urls=("https://a.test", "https://b.test", "https://c.test"))

    first = executor.run(job, max_items=1)
    second = executor.run(job, max_items=1)
    third = executor.run(job)

    assert first.status is JobStatus.PARTIAL
    assert second.status is JobStatus.PARTIAL
    assert third.status is JobStatus.SUCCEEDED
    assert [call[0].url for call in pipeline.calls] == [
        "https://a.test",
        "https://b.test",
        "https://c.test",
    ]
    assert third.items[0].resumed
    assert third.items[0].result is None


def test_review_required_checkpoint_is_terminal_for_resume():
    store = InMemoryJobCheckpointStore()
    pipeline = FakePipeline(["review", "ready"])
    executor = JobExecutor(pipeline, checkpoint_store=store)
    job = make_job(urls=("https://a.test", "https://b.test"))

    first = executor.run(job, max_items=1)
    second = executor.run(job)

    assert first.items[0].status is JobItemStatus.REVIEW_REQUIRED
    assert second.items[0].resumed
    assert [call[0].url for call in pipeline.calls] == [
        "https://a.test",
        "https://b.test",
    ]


def test_checkpoint_definition_mismatch_fails_before_execution():
    store = InMemoryJobCheckpointStore()
    pipeline = FakePipeline(["ready"])
    executor = JobExecutor(pipeline, checkpoint_store=store)
    executor.run(make_job(job_id="same", urls=("https://a.test",)))

    with pytest.raises(JobCheckpointMismatchError):
        executor.run(make_job(job_id="same", urls=("https://b.test",)))


def test_resume_false_restarts_job_from_scratch():
    store = InMemoryJobCheckpointStore()
    pipeline = FakePipeline(["ready", "ready"])
    executor = JobExecutor(pipeline, checkpoint_store=store)
    job = make_job()
    executor.run(job)
    restarted = executor.run(job, resume=False)
    assert len(pipeline.calls) == 2
    assert not restarted.items[0].resumed


def test_interrupted_running_attempt_is_retried_with_remaining_budget():
    store = InMemoryJobCheckpointStore()
    job = make_job()
    checkpoint = JobCheckpoint(
        job_id=job.job_id,
        definition_hash=job.definition_hash,
        items={
            job.items[0].item_id: JobCheckpointItem(
                status=JobItemStatus.RUNNING,
                attempts=1,
                updated_at=1,
            )
        },
    )
    store.save(checkpoint)
    pipeline = FakePipeline(["ready"])
    executor = JobExecutor(
        pipeline,
        retry_policy=RetryPolicy(max_attempts=3),
        checkpoint_store=store,
    )
    result = executor.run(job)
    assert result.items[0].status is JobItemStatus.SUCCEEDED
    assert result.items[0].attempt_count == 2


def test_interrupted_attempt_at_budget_becomes_failed_without_reexecution():
    store = InMemoryJobCheckpointStore()
    job = make_job()
    checkpoint = JobCheckpoint(
        job_id=job.job_id,
        definition_hash=job.definition_hash,
        items={
            job.items[0].item_id: JobCheckpointItem(
                status=JobItemStatus.RUNNING,
                attempts=3,
                updated_at=1,
            )
        },
    )
    store.save(checkpoint)
    pipeline = FakePipeline([])
    executor = JobExecutor(
        pipeline,
        retry_policy=RetryPolicy(max_attempts=3),
        checkpoint_store=store,
    )
    result = executor.run(job)
    assert result.items[0].status is JobItemStatus.FAILED
    assert pipeline.calls == []


def test_json_checkpoint_store_survives_reload_and_contains_no_url_or_headers(tmp_path):
    directory = tmp_path / "jobs"
    store = JsonJobCheckpointStore(directory)
    pipeline = FakePipeline(["ready"])
    executor = JobExecutor(pipeline, checkpoint_store=store)
    job = ExtractionJob(
        "checkpoint-job",
        (JobItem("https://a.test/private", headers={"Authorization": "secret"}),),
        (FieldSpec("price"),),
    )
    executor.run(job)

    reloaded = JsonJobCheckpointStore(directory).load(job.job_id)
    assert reloaded is not None
    assert reloaded.items[job.items[0].item_id].status is JobItemStatus.SUCCEEDED
    text = next(directory.iterdir()).read_text(encoding="utf-8")
    assert "https://a.test/private" not in text
    assert "Authorization" not in text
    assert "secret" not in text


def test_clear_checkpoint_allows_clean_reexecution():
    store = InMemoryJobCheckpointStore()
    pipeline = FakePipeline(["ready", "ready"])
    executor = JobExecutor(pipeline, checkpoint_store=store)
    job = make_job()
    executor.run(job)
    executor.clear_checkpoint(job.job_id)
    result = executor.run(job)
    assert len(pipeline.calls) == 2
    assert result.items[0].status is JobItemStatus.SUCCEEDED


def test_max_items_must_be_positive():
    executor = JobExecutor(FakePipeline([]))
    with pytest.raises(ValueError, match="max_items"):
        executor.run(make_job(), max_items=0)
