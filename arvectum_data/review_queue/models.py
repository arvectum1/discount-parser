from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

REVIEW_QUEUE_SCHEMA_VERSION = 1


class ReviewAction(StrEnum):
    CLAIMED = "claimed"
    TAKEN_OVER = "taken_over"
    RENEWED = "renewed"
    RELEASED = "released"
    SUBMITTED = "submitted"
    COMPLETED = "completed"


class ReviewQueueError(RuntimeError):
    pass


class ReviewLeaseConflictError(ReviewQueueError):
    pass


class ReviewLeaseExpiredError(ReviewQueueError):
    pass


class ReviewLeaseNotFoundError(ReviewQueueError):
    pass


class ReviewerMismatchError(ReviewQueueError):
    pass


@dataclass(frozen=True, slots=True)
class ReviewerIdentity:
    reviewer_id: str
    display_name: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        reviewer_id = self.reviewer_id.strip()
        if not reviewer_id:
            raise ValueError("reviewer_id must not be blank")
        object.__setattr__(self, "reviewer_id", reviewer_id)
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class ReviewLease:
    job_id: str
    item_id: str
    reviewer_id: str
    lease_token: str
    claimed_at: float
    expires_at: float
    updated_at: float
    revision: int = 1
    version: int = REVIEW_QUEUE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.version != REVIEW_QUEUE_SCHEMA_VERSION:
            raise ValueError("Unsupported review lease schema version")
        if not self.job_id or not self.item_id or not self.reviewer_id:
            raise ValueError("ReviewLease identifiers must not be blank")
        if not self.lease_token:
            raise ValueError("lease_token must not be blank")
        if self.revision < 1:
            raise ValueError("lease revision must be >= 1")
        if self.expires_at <= self.claimed_at:
            raise ValueError("lease expires_at must be after claimed_at")
        if self.updated_at < self.claimed_at:
            raise ValueError("lease updated_at must be >= claimed_at")

    def active(self, now: float) -> bool:
        return now < self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "job_id": self.job_id,
            "item_id": self.item_id,
            "reviewer_id": self.reviewer_id,
            "lease_token": self.lease_token,
            "claimed_at": self.claimed_at,
            "expires_at": self.expires_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReviewLease":
        return cls(
            version=int(payload.get("version", -1)),
            job_id=str(payload["job_id"]),
            item_id=str(payload["item_id"]),
            reviewer_id=str(payload["reviewer_id"]),
            lease_token=str(payload["lease_token"]),
            claimed_at=float(payload["claimed_at"]),
            expires_at=float(payload["expires_at"]),
            updated_at=float(payload["updated_at"]),
            revision=int(payload["revision"]),
        )


@dataclass(frozen=True, slots=True)
class ReviewAuditEvent:
    event_id: str
    job_id: str
    item_id: str
    action: ReviewAction
    reviewer_id: str
    at: float
    lease_revision: int | None = None
    result_revision_before: int | None = None
    result_revision_after: int | None = None
    selections: Mapping[str, str | None] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    version: int = REVIEW_QUEUE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.version != REVIEW_QUEUE_SCHEMA_VERSION:
            raise ValueError("Unsupported review audit schema version")
        if not self.event_id.strip():
            raise ValueError("event_id must not be blank")
        if not self.job_id or not self.item_id or not self.reviewer_id:
            raise ValueError("Audit identifiers must not be blank")
        if not isinstance(self.action, ReviewAction):
            object.__setattr__(self, "action", ReviewAction(self.action))
        object.__setattr__(self, "selections", dict(self.selections))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "event_id": self.event_id,
            "job_id": self.job_id,
            "item_id": self.item_id,
            "action": self.action.value,
            "reviewer_id": self.reviewer_id,
            "at": self.at,
            "lease_revision": self.lease_revision,
            "result_revision_before": self.result_revision_before,
            "result_revision_after": self.result_revision_after,
            "selections": dict(self.selections),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReviewAuditEvent":
        return cls(
            version=int(payload.get("version", -1)),
            event_id=str(payload["event_id"]),
            job_id=str(payload["job_id"]),
            item_id=str(payload["item_id"]),
            action=ReviewAction(str(payload["action"])),
            reviewer_id=str(payload["reviewer_id"]),
            at=float(payload["at"]),
            lease_revision=None if payload.get("lease_revision") is None else int(payload["lease_revision"]),
            result_revision_before=None if payload.get("result_revision_before") is None else int(payload["result_revision_before"]),
            result_revision_after=None if payload.get("result_revision_after") is None else int(payload["result_revision_after"]),
            selections={str(k): (None if v is None else str(v)) for k, v in dict(payload.get("selections", {})).items()},
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(frozen=True, slots=True)
class ReviewQueueItem:
    job_id: str
    item_id: str
    result_revision: int
    definition_hash: str
    lease: ReviewLease | None
    available: bool


@dataclass(frozen=True, slots=True)
class ReviewClaim:
    item: ReviewQueueItem
    lease: ReviewLease


@dataclass(frozen=True, slots=True)
class ReviewSubmission:
    record: Any
    result: Any
    checkpoint_synced: bool
    lease_released: bool
