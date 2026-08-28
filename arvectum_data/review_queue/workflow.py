from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Callable, Mapping

from ..execution.checkpoints import JobCheckpointStore
from ..orchestration import URLExtractionPipeline, URLExtractionResult
from ..results.models import ResultConflictError, StoredResultRecord, StoredResultStatus
from ..results.review import DurableReviewCoordinator
from ..results.stores import ResultStore
from .models import (
    ReviewAction,
    ReviewAuditEvent,
    ReviewClaim,
    ReviewLease,
    ReviewLeaseConflictError,
    ReviewLeaseExpiredError,
    ReviewLeaseNotFoundError,
    ReviewQueueItem,
    ReviewerIdentity,
    ReviewerMismatchError,
    ReviewSubmission,
)
from .stores import InMemoryReviewQueueStore, ReviewQueueStore


class GovernedReviewQueue:
    """Lease-governed reviewer workflow over durable review-required results."""

    def __init__(
        self,
        result_store: ResultStore,
        *,
        queue_store: ReviewQueueStore | None = None,
        pipeline: URLExtractionPipeline | None = None,
        checkpoint_store: JobCheckpointStore | None = None,
        review_coordinator: DurableReviewCoordinator | None = None,
        default_lease_s: float = 15 * 60,
        max_lease_s: float = 24 * 60 * 60,
        clock: Callable[[], float] | None = None,
        token_factory: Callable[[], str] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if default_lease_s <= 0:
            raise ValueError("default_lease_s must be positive")
        if max_lease_s < default_lease_s:
            raise ValueError("max_lease_s must be >= default_lease_s")
        self.result_store = result_store
        self.queue_store = queue_store or InMemoryReviewQueueStore()
        self._clock = clock or time.time
        self.default_lease_s = default_lease_s
        self.max_lease_s = max_lease_s
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(24))
        self._event_id_factory = event_id_factory or (lambda: uuid.uuid4().hex)
        self.review = review_coordinator or DurableReviewCoordinator(
            result_store,
            pipeline=pipeline,
            checkpoint_store=checkpoint_store,
            clock=self._clock,
        )

    def _ttl(self, requested: float | None) -> float:
        ttl = self.default_lease_s if requested is None else float(requested)
        if ttl <= 0:
            raise ValueError("lease_s must be positive")
        if ttl > self.max_lease_s:
            raise ValueError("lease_s exceeds max_lease_s")
        return ttl

    @staticmethod
    def _identity(reviewer: ReviewerIdentity | str) -> ReviewerIdentity:
        return reviewer if isinstance(reviewer, ReviewerIdentity) else ReviewerIdentity(str(reviewer))

    def pending(self, *, job_id: str | None = None, include_claimed: bool = False) -> tuple[ReviewQueueItem, ...]:
        now = self._clock()
        items = []
        for record in self.review.pending(job_id=job_id):
            lease = self.queue_store.load_lease(record.job_id, record.item_id)
            available = lease is None or not lease.active(now)
            if not include_claimed and not available:
                continue
            items.append(
                ReviewQueueItem(
                    job_id=record.job_id,
                    item_id=record.item_id,
                    result_revision=record.revision,
                    definition_hash=record.definition_hash,
                    lease=lease,
                    available=available,
                )
            )
        return tuple(items)

    def claim(self, job_id: str, item_id: str, reviewer: ReviewerIdentity | str, *, expected_result_revision: int | None = None, lease_s: float | None = None) -> ReviewClaim:
        identity = self._identity(reviewer)
        record, _ = self.review.get(job_id, item_id)
        if record.status is not StoredResultStatus.REVIEW_REQUIRED:
            raise ValueError("Only review-required results may be claimed")
        if expected_result_revision is not None and record.revision != expected_result_revision:
            raise ResultConflictError("Durable review revision conflict before claim")
        token = self._token_factory()
        if not token:
            raise RuntimeError("token_factory returned an empty lease token")
        now = self._clock()
        lease, _event = self.queue_store.claim(
            job_id,
            item_id,
            identity.reviewer_id,
            token,
            now=now,
            ttl_s=self._ttl(lease_s),
            event_id=self._event_id_factory(),
        )
        item = ReviewQueueItem(
            job_id=record.job_id,
            item_id=record.item_id,
            result_revision=record.revision,
            definition_hash=record.definition_hash,
            lease=lease,
            available=False,
        )
        return ReviewClaim(item=item, lease=lease)

    def claim_next(self, reviewer: ReviewerIdentity | str, *, job_id: str | None = None, lease_s: float | None = None) -> ReviewClaim | None:
        identity = self._identity(reviewer)
        for item in self.pending(job_id=job_id, include_claimed=True):
            lease = item.lease
            if lease is not None and lease.active(self._clock()) and lease.reviewer_id != identity.reviewer_id:
                continue
            try:
                return self.claim(
                    item.job_id,
                    item.item_id,
                    identity,
                    expected_result_revision=item.result_revision,
                    lease_s=lease_s,
                )
            except (ReviewLeaseConflictError, ResultConflictError):
                continue
        return None

    def get_claim(self, job_id: str, item_id: str, reviewer: ReviewerIdentity | str, lease_token: str) -> tuple[ReviewLease, StoredResultRecord, URLExtractionResult]:
        identity = self._identity(reviewer)
        lease = self._require_active_lease(job_id, item_id, identity.reviewer_id, lease_token)
        record, result = self.review.get(job_id, item_id)
        if record.status is not StoredResultStatus.REVIEW_REQUIRED:
            raise ValueError("Claimed result no longer requires review")
        return lease, record, result

    def renew(self, job_id: str, item_id: str, reviewer: ReviewerIdentity | str, lease_token: str, *, lease_s: float | None = None) -> ReviewLease:
        identity = self._identity(reviewer)
        lease, _event = self.queue_store.renew(
            job_id,
            item_id,
            identity.reviewer_id,
            lease_token,
            now=self._clock(),
            ttl_s=self._ttl(lease_s),
            event_id=self._event_id_factory(),
        )
        return lease

    def release(self, job_id: str, item_id: str, reviewer: ReviewerIdentity | str, lease_token: str) -> None:
        identity = self._identity(reviewer)
        self.queue_store.release(
            job_id,
            item_id,
            identity.reviewer_id,
            lease_token,
            now=self._clock(),
            event_id=self._event_id_factory(),
        )

    def submit(
        self,
        job_id: str,
        item_id: str,
        reviewer: ReviewerIdentity | str,
        lease_token: str,
        selections: Mapping[str, str | None],
        *,
        expected_result_revision: int,
        expected_definition_hash: str | None = None,
    ) -> ReviewSubmission:
        if not selections:
            raise ValueError("selections must not be empty")
        identity = self._identity(reviewer)
        lease = self._require_active_lease(job_id, item_id, identity.reviewer_id, lease_token)
        record, _ = self.review.get(job_id, item_id, expected_definition_hash=expected_definition_hash)
        if record.revision != expected_result_revision:
            raise ResultConflictError("Durable review revision conflict")
        update = self.review.confirm(
            job_id,
            item_id,
            selections,
            expected_revision=expected_result_revision,
            expected_definition_hash=expected_definition_hash,
        )
        submitted = ReviewAuditEvent(
            event_id=self._event_id_factory(),
            job_id=job_id,
            item_id=item_id,
            action=ReviewAction.SUBMITTED,
            reviewer_id=identity.reviewer_id,
            at=self._clock(),
            lease_revision=lease.revision,
            result_revision_before=record.revision,
            result_revision_after=update.record.revision,
            selections={str(key): value for key, value in selections.items()},
            metadata={"checkpoint_synced": update.checkpoint_synced},
        )
        self.queue_store.append_event(submitted)

        released = False
        if update.record.status is not StoredResultStatus.REVIEW_REQUIRED:
            self.queue_store.release(
                job_id,
                item_id,
                identity.reviewer_id,
                lease_token,
                now=self._clock(),
                event_id=self._event_id_factory(),
                completed=True,
            )
            released = True
        return ReviewSubmission(
            record=update.record,
            result=update.result,
            checkpoint_synced=update.checkpoint_synced,
            lease_released=released,
        )

    def reject_fields(self, job_id: str, item_id: str, reviewer: ReviewerIdentity | str, lease_token: str, field_keys: tuple[str, ...] | list[str], *, expected_result_revision: int, expected_definition_hash: str | None = None) -> ReviewSubmission:
        if not field_keys:
            raise ValueError("field_keys must not be empty")
        return self.submit(
            job_id,
            item_id,
            reviewer,
            lease_token,
            {str(key): None for key in field_keys},
            expected_result_revision=expected_result_revision,
            expected_definition_hash=expected_definition_hash,
        )

    def history(self, *, job_id: str | None = None, item_id: str | None = None) -> tuple[ReviewAuditEvent, ...]:
        return tuple(self.queue_store.list_events(job_id=job_id, item_id=item_id))

    def _require_active_lease(self, job_id: str, item_id: str, reviewer_id: str, lease_token: str) -> ReviewLease:
        lease = self.queue_store.load_lease(job_id, item_id)
        if lease is None:
            raise ReviewLeaseNotFoundError("Review lease does not exist")
        if lease.reviewer_id != reviewer_id:
            raise ReviewerMismatchError("Review lease belongs to another reviewer")
        if lease.lease_token != lease_token:
            raise ReviewLeaseConflictError("Review lease token does not match")
        if not lease.active(self._clock()):
            raise ReviewLeaseExpiredError("Review lease has expired")
        return lease
