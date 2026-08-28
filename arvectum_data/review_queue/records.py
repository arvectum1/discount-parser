from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..engine import RecordExtractionResult, RecordStatus
from ..results.models import ResultConflictError
from ..results.record_sets import (
    DurableRecordReviewCoordinator,
    RecordReviewUpdate,
    RecordResultRepository,
    StoredRecordResult,
    record_storage_item_id,
)
from ..results.stores import ResultStore
from .models import (
    ReviewAction,
    ReviewAuditEvent,
    ReviewLease,
    ReviewLeaseConflictError,
    ReviewLeaseExpiredError,
    ReviewLeaseNotFoundError,
    ReviewerIdentity,
    ReviewerMismatchError,
)
from .stores import InMemoryReviewQueueStore, ReviewQueueStore


@dataclass(frozen=True, slots=True)
class RecordReviewQueueItem:
    job_id: str
    item_id: str
    record_id: str
    result_revision: int
    definition_hash: str
    lease: ReviewLease | None
    available: bool


@dataclass(frozen=True, slots=True)
class RecordReviewClaim:
    item: RecordReviewQueueItem
    lease: ReviewLease


@dataclass(frozen=True, slots=True)
class RecordReviewSubmission:
    record: StoredRecordResult
    result: RecordExtractionResult
    lease_released: bool


class GovernedRecordReviewQueue:
    """Lease-governed record-scoped review over DP-015 durable record results.

    The existing DP-009 queue backends are reused with a reserved reversible
    storage item id, so sibling records from one source item can be claimed and
    reviewed independently without any schema migration or cross-record lease.
    """

    def __init__(
        self,
        result_store: ResultStore,
        *,
        queue_store: ReviewQueueStore | None = None,
        repository: RecordResultRepository | None = None,
        review_coordinator: DurableRecordReviewCoordinator | None = None,
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
        self.repository = repository or RecordResultRepository(result_store, clock=self._clock)
        self.review = review_coordinator or DurableRecordReviewCoordinator(
            result_store,
            repository=self.repository,
            clock=self._clock,
        )

    @staticmethod
    def _identity(reviewer: ReviewerIdentity | str) -> ReviewerIdentity:
        return reviewer if isinstance(reviewer, ReviewerIdentity) else ReviewerIdentity(str(reviewer))

    def _ttl(self, requested: float | None) -> float:
        ttl = self.default_lease_s if requested is None else float(requested)
        if ttl <= 0:
            raise ValueError("lease_s must be positive")
        if ttl > self.max_lease_s:
            raise ValueError("lease_s exceeds max_lease_s")
        return ttl

    @staticmethod
    def _queue_item_id(item_id: str, record_id: str) -> str:
        return record_storage_item_id(item_id, record_id)

    def pending(
        self,
        *,
        job_id: str | None = None,
        item_id: str | None = None,
        include_claimed: bool = False,
    ) -> tuple[RecordReviewQueueItem, ...]:
        now = self._clock()
        items: list[RecordReviewQueueItem] = []
        for record in self.review.pending(job_id=job_id, item_id=item_id):
            queue_item_id = self._queue_item_id(record.item_id, record.record_id)
            lease = self.queue_store.load_lease(record.job_id, queue_item_id)
            available = lease is None or not lease.active(now)
            if not include_claimed and not available:
                continue
            items.append(
                RecordReviewQueueItem(
                    job_id=record.job_id,
                    item_id=record.item_id,
                    record_id=record.record_id,
                    result_revision=record.revision,
                    definition_hash=record.definition_hash,
                    lease=lease,
                    available=available,
                )
            )
        return tuple(items)

    def claim(
        self,
        job_id: str,
        item_id: str,
        record_id: str,
        reviewer: ReviewerIdentity | str,
        *,
        expected_result_revision: int | None = None,
        lease_s: float | None = None,
    ) -> RecordReviewClaim:
        identity = self._identity(reviewer)
        record, _ = self.review.get(job_id, item_id, record_id)
        if record.status is not RecordStatus.NEEDS_CONFIRMATION:
            raise ValueError("Only review-required durable records may be claimed")
        if expected_result_revision is not None and record.revision != expected_result_revision:
            raise ResultConflictError("Durable record review revision conflict before claim")
        token = self._token_factory()
        if not token:
            raise RuntimeError("token_factory returned an empty lease token")
        queue_item_id = self._queue_item_id(item_id, record_id)
        lease, _event = self.queue_store.claim(
            job_id,
            queue_item_id,
            identity.reviewer_id,
            token,
            now=self._clock(),
            ttl_s=self._ttl(lease_s),
            event_id=self._event_id_factory(),
        )
        return RecordReviewClaim(
            item=RecordReviewQueueItem(
                job_id=job_id,
                item_id=item_id,
                record_id=record_id,
                result_revision=record.revision,
                definition_hash=record.definition_hash,
                lease=lease,
                available=False,
            ),
            lease=lease,
        )

    def claim_next(
        self,
        reviewer: ReviewerIdentity | str,
        *,
        job_id: str | None = None,
        item_id: str | None = None,
        lease_s: float | None = None,
    ) -> RecordReviewClaim | None:
        identity = self._identity(reviewer)
        for item in self.pending(
            job_id=job_id,
            item_id=item_id,
            include_claimed=True,
        ):
            lease = item.lease
            if (
                lease is not None
                and lease.active(self._clock())
                and lease.reviewer_id != identity.reviewer_id
            ):
                continue
            try:
                return self.claim(
                    item.job_id,
                    item.item_id,
                    item.record_id,
                    identity,
                    expected_result_revision=item.result_revision,
                    lease_s=lease_s,
                )
            except (ReviewLeaseConflictError, ResultConflictError):
                continue
        return None

    def get_claim(
        self,
        job_id: str,
        item_id: str,
        record_id: str,
        reviewer: ReviewerIdentity | str,
        lease_token: str,
    ) -> tuple[ReviewLease, StoredRecordResult, RecordExtractionResult]:
        identity = self._identity(reviewer)
        lease = self._require_active_lease(
            job_id,
            item_id,
            record_id,
            identity.reviewer_id,
            lease_token,
        )
        record, result = self.review.get(job_id, item_id, record_id)
        if record.status is not RecordStatus.NEEDS_CONFIRMATION:
            raise ValueError("Claimed durable record no longer requires review")
        return lease, record, result

    def renew(
        self,
        job_id: str,
        item_id: str,
        record_id: str,
        reviewer: ReviewerIdentity | str,
        lease_token: str,
        *,
        lease_s: float | None = None,
    ) -> ReviewLease:
        identity = self._identity(reviewer)
        lease, _event = self.queue_store.renew(
            job_id,
            self._queue_item_id(item_id, record_id),
            identity.reviewer_id,
            lease_token,
            now=self._clock(),
            ttl_s=self._ttl(lease_s),
            event_id=self._event_id_factory(),
        )
        return lease

    def release(
        self,
        job_id: str,
        item_id: str,
        record_id: str,
        reviewer: ReviewerIdentity | str,
        lease_token: str,
    ) -> None:
        identity = self._identity(reviewer)
        self.queue_store.release(
            job_id,
            self._queue_item_id(item_id, record_id),
            identity.reviewer_id,
            lease_token,
            now=self._clock(),
            event_id=self._event_id_factory(),
        )

    def submit_fields(
        self,
        job_id: str,
        item_id: str,
        record_id: str,
        reviewer: ReviewerIdentity | str,
        lease_token: str,
        selections: Mapping[str, str | None],
        *,
        expected_result_revision: int,
        expected_definition_hash: str | None = None,
    ) -> RecordReviewSubmission:
        if not selections:
            raise ValueError("selections must not be empty")
        identity = self._identity(reviewer)
        lease = self._require_active_lease(
            job_id,
            item_id,
            record_id,
            identity.reviewer_id,
            lease_token,
        )
        record, _ = self.review.get(
            job_id,
            item_id,
            record_id,
            expected_definition_hash=expected_definition_hash,
        )
        if record.revision != expected_result_revision:
            raise ResultConflictError("Durable record review revision conflict")
        update = self.review.confirm_fields(
            job_id,
            item_id,
            record_id,
            selections,
            expected_revision=expected_result_revision,
            expected_definition_hash=expected_definition_hash,
        )
        self._append_submission(
            lease,
            identity,
            item_id=item_id,
            record_id=record_id,
            revision_before=record.revision,
            update=update,
            selections={str(key): value for key, value in selections.items()},
            scope="record_fields",
        )
        released = self._complete_if_terminal(
            update.record,
            identity,
            lease_token,
        )
        return RecordReviewSubmission(update.record, update.result, released)

    def submit_boundary(
        self,
        job_id: str,
        item_id: str,
        record_id: str,
        reviewer: ReviewerIdentity | str,
        lease_token: str,
        *,
        accept: bool,
        expected_result_revision: int,
        expected_definition_hash: str | None = None,
    ) -> RecordReviewSubmission:
        identity = self._identity(reviewer)
        lease = self._require_active_lease(
            job_id,
            item_id,
            record_id,
            identity.reviewer_id,
            lease_token,
        )
        record, _ = self.review.get(
            job_id,
            item_id,
            record_id,
            expected_definition_hash=expected_definition_hash,
        )
        if record.revision != expected_result_revision:
            raise ResultConflictError("Durable record review revision conflict")
        update = self.review.confirm_boundary(
            job_id,
            item_id,
            record_id,
            accept=accept,
            expected_revision=expected_result_revision,
            expected_definition_hash=expected_definition_hash,
        )
        self._append_submission(
            lease,
            identity,
            item_id=item_id,
            record_id=record_id,
            revision_before=record.revision,
            update=update,
            selections={"$record_boundary": "accept" if accept else None},
            scope="record_boundary",
        )
        released = self._complete_if_terminal(
            update.record,
            identity,
            lease_token,
        )
        return RecordReviewSubmission(update.record, update.result, released)

    def history(
        self,
        *,
        job_id: str | None = None,
        item_id: str | None = None,
        record_id: str | None = None,
    ) -> tuple[ReviewAuditEvent, ...]:
        events = tuple(self.queue_store.list_events(job_id=job_id))
        if item_id is None and record_id is None:
            return tuple(
                event
                for event in events
                if event.item_id.startswith("__dp_record_v1__:")
            )
        filtered: list[ReviewAuditEvent] = []
        for event in events:
            metadata_item = event.metadata.get("parent_item_id")
            metadata_record = event.metadata.get("record_id")
            if metadata_item is not None or metadata_record is not None:
                if item_id is not None and metadata_item != item_id:
                    continue
                if record_id is not None and metadata_record != record_id:
                    continue
                filtered.append(event)
                continue
            # Lease lifecycle events created by the underlying DP-009 store do not
            # carry metadata, but their reversible queue item id is authoritative.
            if item_id is not None and record_id is not None:
                if event.item_id == self._queue_item_id(item_id, record_id):
                    filtered.append(event)
        return tuple(filtered)

    def _append_submission(
        self,
        lease: ReviewLease,
        identity: ReviewerIdentity,
        *,
        item_id: str,
        record_id: str,
        revision_before: int,
        update: RecordReviewUpdate,
        selections: Mapping[str, str | None],
        scope: str,
    ) -> None:
        self.queue_store.append_event(
            ReviewAuditEvent(
                event_id=self._event_id_factory(),
                job_id=update.record.job_id,
                item_id=self._queue_item_id(item_id, record_id),
                action=ReviewAction.SUBMITTED,
                reviewer_id=identity.reviewer_id,
                at=self._clock(),
                lease_revision=lease.revision,
                result_revision_before=revision_before,
                result_revision_after=update.record.revision,
                selections=dict(selections),
                metadata={
                    "parent_item_id": item_id,
                    "record_id": record_id,
                    "review_scope": scope,
                },
            )
        )

    def _complete_if_terminal(
        self,
        record: StoredRecordResult,
        identity: ReviewerIdentity,
        lease_token: str,
    ) -> bool:
        if record.status is RecordStatus.NEEDS_CONFIRMATION:
            return False
        self.queue_store.release(
            record.job_id,
            self._queue_item_id(record.item_id, record.record_id),
            identity.reviewer_id,
            lease_token,
            now=self._clock(),
            event_id=self._event_id_factory(),
            completed=True,
        )
        return True

    def _require_active_lease(
        self,
        job_id: str,
        item_id: str,
        record_id: str,
        reviewer_id: str,
        lease_token: str,
    ) -> ReviewLease:
        lease = self.queue_store.load_lease(
            job_id,
            self._queue_item_id(item_id, record_id),
        )
        if lease is None:
            raise ReviewLeaseNotFoundError("Record review lease does not exist")
        if lease.reviewer_id != reviewer_id:
            raise ReviewerMismatchError("Record review lease belongs to another reviewer")
        if lease.lease_token != lease_token:
            raise ReviewLeaseConflictError("Record review lease token does not match")
        if not lease.active(self._clock()):
            raise ReviewLeaseExpiredError("Record review lease has expired")
        return lease
