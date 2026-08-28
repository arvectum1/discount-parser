from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

from ..engine import FieldStatus
from ..orchestration import URLExtractionPipeline, URLExtractionResult
from ..results import ResultCodec, ResultRepository, ResultStore
from .checkpoints import InMemoryJobCheckpointStore, JobCheckpointStore
from .models import (
    ExtractionJob,
    JobAttempt,
    JobCheckpoint,
    JobCheckpointItem,
    JobCheckpointMismatchError,
    JobError,
    JobItem,
    JobItemResult,
    JobItemStatus,
    JobRunResult,
    RetryPolicy,
)


class JobExecutor:
    """Sequential resumable job runner above URLExtractionPipeline."""

    def __init__(
        self,
        pipeline: URLExtractionPipeline | None = None,
        *,
        retry_policy: RetryPolicy | None = None,
        checkpoint_store: JobCheckpointStore | None = None,
        result_store: ResultStore | None = None,
        result_codec: ResultCodec | None = None,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.pipeline = pipeline if pipeline is not None else URLExtractionPipeline()
        self.retry_policy = retry_policy if retry_policy is not None else RetryPolicy()
        self.checkpoint_store = (
            checkpoint_store
            if checkpoint_store is not None
            else InMemoryJobCheckpointStore()
        )
        self._sleep = sleeper if sleeper is not None else time.sleep
        self._clock = clock if clock is not None else time.time
        self.result_repository = (
            ResultRepository(
                result_store,
                codec=result_codec,
                clock=self._clock,
            )
            if result_store is not None
            else None
        )

    def run(
        self,
        job: ExtractionJob,
        *,
        resume: bool = True,
        max_items: int | None = None,
    ) -> JobRunResult:
        if max_items is not None and max_items < 1:
            raise ValueError("max_items must be >= 1")
        if not resume and self.result_repository is not None:
            self.result_repository.clear_job(job.job_id)
        started_at = self._clock()
        checkpoint = self._load_checkpoint(job, resume=resume)
        current: dict[str, JobItemResult] = {}
        processed = 0

        for item in job.items:
            state = checkpoint.items[item.item_id]
            if state.status.terminal:
                continue
            if max_items is not None and processed >= max_items:
                continue
            processed += 1
            result, checkpoint = self._execute_item(job, item, checkpoint)
            current[item.item_id] = result

        items: list[JobItemResult] = []
        for item in job.items:
            if item.item_id in current:
                items.append(current[item.item_id])
                continue
            state = checkpoint.items[item.item_id]
            status = JobItemStatus.PENDING if state.status is JobItemStatus.RUNNING else state.status
            durable_result = self._load_durable_result(job, item) if state.status.terminal else None
            items.append(
                JobItemResult(
                    item=item,
                    status=status,
                    attempt_count=state.attempts,
                    result=durable_result,
                    error=state.last_error,
                    review_fields=state.review_fields,
                    unresolved_required_fields=state.unresolved_required_fields,
                    resumed=state.status.terminal,
                )
            )

        return JobRunResult(
            job=job,
            items=tuple(items),
            started_at=started_at,
            finished_at=self._clock(),
            checkpoint_revision=checkpoint.revision,
        )

    def clear_checkpoint(self, job_id: str) -> None:
        self.checkpoint_store.clear(job_id)

    def clear_results(self, job_id: str) -> None:
        if self.result_repository is not None:
            self.result_repository.clear_job(job_id)

    def _load_checkpoint(self, job: ExtractionJob, *, resume: bool) -> JobCheckpoint:
        loaded = self.checkpoint_store.load(job.job_id) if resume else None
        if loaded is not None:
            if loaded.definition_hash != job.definition_hash:
                raise JobCheckpointMismatchError(
                    "Existing checkpoint does not match current job definition"
                )
            if set(loaded.items) != {item.item_id for item in job.items}:
                raise JobCheckpointMismatchError(
                    "Existing checkpoint item set does not match current job"
                )
            return loaded

        now = self._clock()
        checkpoint = JobCheckpoint(
            job_id=job.job_id,
            definition_hash=job.definition_hash,
            items={
                item.item_id: JobCheckpointItem(updated_at=now)
                for item in job.items
            },
            updated_at=now,
        )
        self.checkpoint_store.save(checkpoint)
        return checkpoint

    def _replace_state(
        self,
        checkpoint: JobCheckpoint,
        item_id: str,
        state: JobCheckpointItem,
    ) -> JobCheckpoint:
        items = dict(checkpoint.items)
        items[item_id] = state
        updated = replace(
            checkpoint,
            items=items,
            revision=checkpoint.revision + 1,
            updated_at=self._clock(),
        )
        self.checkpoint_store.save(updated)
        return updated

    def _execute_item(
        self,
        job: ExtractionJob,
        item: JobItem,
        checkpoint: JobCheckpoint,
    ) -> tuple[JobItemResult, JobCheckpoint]:
        initial = checkpoint.items[item.item_id]
        attempts: list[JobAttempt] = []

        recovered = self._recover_durable_result(job, item, initial, checkpoint)
        if recovered is not None:
            return recovered

        if (
            initial.status is JobItemStatus.RUNNING
            and initial.attempts >= self.retry_policy.max_attempts
        ):
            return self._exhausted_interrupted(item, initial, checkpoint)

        attempt_number = initial.attempts
        while attempt_number < self.retry_policy.max_attempts:
            attempt_number += 1
            started = self._clock()
            checkpoint = self._replace_state(
                checkpoint,
                item.item_id,
                JobCheckpointItem(
                    status=JobItemStatus.RUNNING,
                    attempts=attempt_number,
                    last_error=initial.last_error,
                    updated_at=started,
                ),
            )
            try:
                extraction = self.pipeline.extract(
                    item.acquisition_request(),
                    job.fields,
                )
            except Exception as exc:
                finished = self._clock()
                retryable = self.retry_policy.is_retryable(exc)
                error = JobError.from_exception(exc, retryable=retryable)
                attempts.append(
                    JobAttempt(
                        attempt_number,
                        started,
                        finished,
                        False,
                        retryable,
                        error,
                    )
                )
                can_retry = retryable and attempt_number < self.retry_policy.max_attempts
                initial = JobCheckpointItem(
                    status=JobItemStatus.PENDING if can_retry else JobItemStatus.FAILED,
                    attempts=attempt_number,
                    last_error=error,
                    updated_at=finished,
                )
                checkpoint = self._replace_state(
                    checkpoint,
                    item.item_id,
                    initial,
                )
                if not can_retry:
                    return (
                        JobItemResult(
                            item,
                            JobItemStatus.FAILED,
                            attempt_number,
                            tuple(attempts),
                            error=error,
                        ),
                        checkpoint,
                    )
                self._sleep(self.retry_policy.delay_after(attempt_number))
                continue

            finished = self._clock()
            attempts.append(JobAttempt(attempt_number, started, finished, True))
            status, review_fields, unresolved = self._classify_result(extraction)

            if self.result_repository is not None:
                self.result_repository.persist_initial(
                    job_id=job.job_id,
                    item_id=item.item_id,
                    definition_hash=job.definition_hash,
                    result=extraction,
                )

            checkpoint = self._replace_state(
                checkpoint,
                item.item_id,
                JobCheckpointItem(
                    status=status,
                    attempts=attempt_number,
                    review_fields=review_fields,
                    unresolved_required_fields=unresolved,
                    updated_at=finished,
                ),
            )
            return (
                JobItemResult(
                    item,
                    status,
                    attempt_number,
                    tuple(attempts),
                    result=extraction,
                    review_fields=review_fields,
                    unresolved_required_fields=unresolved,
                ),
                checkpoint,
            )

        error = initial.last_error or JobError(
            "RetryBudgetExhausted",
            "Retry budget exhausted before item execution could resume",
            False,
        )
        failed = replace(
            initial,
            status=JobItemStatus.FAILED,
            last_error=error,
            updated_at=self._clock(),
        )
        checkpoint = self._replace_state(checkpoint, item.item_id, failed)
        return (
            JobItemResult(
                item,
                JobItemStatus.FAILED,
                failed.attempts,
                error=error,
                resumed=True,
            ),
            checkpoint,
        )

    def _recover_durable_result(
        self,
        job: ExtractionJob,
        item: JobItem,
        state: JobCheckpointItem,
        checkpoint: JobCheckpoint,
    ) -> tuple[JobItemResult, JobCheckpoint] | None:
        if self.result_repository is None or state.status is not JobItemStatus.RUNNING:
            return None
        loaded = self.result_repository.load_result(
            job.job_id,
            item.item_id,
            expected_definition_hash=job.definition_hash,
        )
        if loaded is None:
            return None
        _, extraction = loaded
        status, review_fields, unresolved = self._classify_result(extraction)
        now = self._clock()
        checkpoint = self._replace_state(
            checkpoint,
            item.item_id,
            JobCheckpointItem(
                status=status,
                attempts=state.attempts,
                review_fields=review_fields,
                unresolved_required_fields=unresolved,
                updated_at=now,
            ),
        )
        return (
            JobItemResult(
                item=item,
                status=status,
                attempt_count=state.attempts,
                result=extraction,
                review_fields=review_fields,
                unresolved_required_fields=unresolved,
                resumed=True,
            ),
            checkpoint,
        )

    def _load_durable_result(
        self,
        job: ExtractionJob,
        item: JobItem,
    ) -> URLExtractionResult | None:
        if self.result_repository is None:
            return None
        loaded = self.result_repository.load_result(
            job.job_id,
            item.item_id,
            expected_definition_hash=job.definition_hash,
        )
        return None if loaded is None else loaded[1]

    @staticmethod
    def _classify_result(
        extraction: URLExtractionResult,
    ) -> tuple[JobItemStatus, tuple[str, ...], tuple[str, ...]]:
        if extraction.requires_confirmation:
            review_fields = tuple(
                key
                for key, decision in extraction.extraction.decisions.items()
                if decision.status is FieldStatus.NEEDS_CONFIRMATION
            )
            return (
                JobItemStatus.REVIEW_REQUIRED,
                review_fields,
                extraction.unresolved_required_fields,
            )
        if extraction.unresolved_required_fields:
            return (
                JobItemStatus.INCOMPLETE,
                (),
                extraction.unresolved_required_fields,
            )
        return JobItemStatus.SUCCEEDED, (), ()

    def _exhausted_interrupted(
        self,
        item: JobItem,
        state: JobCheckpointItem,
        checkpoint: JobCheckpoint,
    ) -> tuple[JobItemResult, JobCheckpoint]:
        error = state.last_error or JobError(
            "InterruptedAttempt",
            "Retry budget exhausted by an interrupted prior attempt",
            False,
        )
        failed = replace(
            state,
            status=JobItemStatus.FAILED,
            last_error=error,
            updated_at=self._clock(),
        )
        checkpoint = self._replace_state(checkpoint, item.item_id, failed)
        return (
            JobItemResult(
                item,
                JobItemStatus.FAILED,
                failed.attempts,
                error=error,
                resumed=True,
            ),
            checkpoint,
        )
