from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from ..engine import FieldStatus
from ..execution.checkpoints import JobCheckpointStore
from ..execution.models import JobCheckpointItem, JobItemStatus
from ..orchestration import URLExtractionPipeline, URLExtractionResult
from .models import (
    ResultConflictError,
    ResultDefinitionMismatchError,
    ResultNotFoundError,
    StoredResultRecord,
    StoredResultStatus,
)
from .stores import ResultRepository, ResultStore


@dataclass(frozen=True, slots=True)
class ReviewUpdate:
    record: StoredResultRecord
    result: URLExtractionResult
    checkpoint_synced: bool


class DurableReviewCoordinator:
    """Continue persisted review work without reacquiring the source URL."""

    def __init__(
        self,
        result_store: ResultStore,
        *,
        pipeline: URLExtractionPipeline | None = None,
        checkpoint_store: JobCheckpointStore | None = None,
        repository: ResultRepository | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.pipeline = pipeline if pipeline is not None else URLExtractionPipeline()
        self._clock = clock or time.time
        self.repository = repository or ResultRepository(
            result_store,
            clock=self._clock,
        )
        self.checkpoint_store = checkpoint_store

    def pending(self, *, job_id: str | None = None) -> tuple[StoredResultRecord, ...]:
        return self.repository.pending_reviews(job_id=job_id)

    def get(
        self,
        job_id: str,
        item_id: str,
        *,
        expected_definition_hash: str | None = None,
    ) -> tuple[StoredResultRecord, URLExtractionResult]:
        loaded = self.repository.load_result(
            job_id,
            item_id,
            expected_definition_hash=expected_definition_hash,
        )
        if loaded is None:
            raise ResultNotFoundError("Durable result does not exist")
        return loaded

    def confirm(
        self,
        job_id: str,
        item_id: str,
        selections: Mapping[str, str | None],
        *,
        expected_revision: int | None = None,
        expected_definition_hash: str | None = None,
    ) -> ReviewUpdate:
        record, result = self.get(
            job_id,
            item_id,
            expected_definition_hash=expected_definition_hash,
        )
        if record.status is not StoredResultStatus.REVIEW_REQUIRED:
            raise ValueError(
                f"Durable result is {record.status.value}; only review-required results may be confirmed"
            )
        if expected_revision is not None and record.revision != expected_revision:
            raise ResultConflictError("Durable review revision conflict")

        confirmed = self.pipeline.confirm(result, selections)
        updated = self.repository.update_result(
            record,
            confirmed,
            expected_revision=record.revision,
        )
        synced = self._sync_checkpoint(updated, confirmed)
        return ReviewUpdate(
            record=updated,
            result=confirmed,
            checkpoint_synced=synced,
        )

    def reconcile_checkpoint(self, job_id: str, item_id: str) -> bool:
        record, result = self.get(job_id, item_id)
        return self._sync_checkpoint(record, result)

    def _sync_checkpoint(
        self,
        record: StoredResultRecord,
        result: URLExtractionResult,
    ) -> bool:
        if self.checkpoint_store is None:
            return False
        checkpoint = self.checkpoint_store.load(record.job_id)
        if checkpoint is None:
            return False
        if checkpoint.definition_hash != record.definition_hash:
            raise ResultDefinitionMismatchError(
                "Checkpoint definition does not match durable review result"
            )
        state = checkpoint.items.get(record.item_id)
        if state is None:
            raise ResultDefinitionMismatchError(
                "Checkpoint does not contain durable review item"
            )

        review_fields = tuple(
            key
            for key, decision in result.extraction.decisions.items()
            if decision.status is FieldStatus.NEEDS_CONFIRMATION
        )
        if record.status is StoredResultStatus.READY:
            status = JobItemStatus.SUCCEEDED
            unresolved = ()
        elif record.status is StoredResultStatus.REVIEW_REQUIRED:
            status = JobItemStatus.REVIEW_REQUIRED
            unresolved = result.unresolved_required_fields
        else:
            status = JobItemStatus.INCOMPLETE
            unresolved = result.unresolved_required_fields
            review_fields = ()

        now = self._clock()
        items = dict(checkpoint.items)
        items[record.item_id] = JobCheckpointItem(
            status=status,
            attempts=state.attempts,
            review_fields=review_fields,
            unresolved_required_fields=unresolved,
            last_error=None,
            updated_at=now,
        )
        updated = replace(
            checkpoint,
            items=items,
            revision=checkpoint.revision + 1,
            updated_at=now,
        )
        self.checkpoint_store.save(updated)
        return True
