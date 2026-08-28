from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..acquisition import AcquisitionError, AcquisitionRequest, RenderMode
from ..engine import FieldSpec
from ..orchestration import URLExtractionResult

JOB_CHECKPOINT_VERSION = 1
MAX_ERROR_MESSAGE = 2_000
_URL_IN_ERROR_RE = re.compile(r"(?i)https?://[^\s\'\"<>]+")


class JobItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    REVIEW_REQUIRED = "review_required"
    INCOMPLETE = "incomplete"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            JobItemStatus.SUCCEEDED,
            JobItemStatus.REVIEW_REQUIRED,
            JobItemStatus.INCOMPLETE,
            JobItemStatus.FAILED,
        }


class JobStatus(StrEnum):
    SUCCEEDED = "succeeded"
    NEEDS_ATTENTION = "needs_attention"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class JobItem:
    url: str
    item_id: str = ""
    asset_id: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float = 20.0
    max_bytes: int = 5_000_000
    render_mode: RenderMode = RenderMode.AUTO

    def __post_init__(self) -> None:
        headers = dict(self.headers)
        request = AcquisitionRequest(
            url=self.url,
            asset_id=self.asset_id,
            headers=headers,
            timeout_s=self.timeout_s,
            max_bytes=self.max_bytes,
            render_mode=self.render_mode,
        )
        object.__setattr__(self, "headers", headers)
        object.__setattr__(self, "render_mode", request.render_mode)
        cleaned = self.item_id.strip()
        if cleaned:
            object.__setattr__(self, "item_id", cleaned)
        else:
            digest = hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:20]
            object.__setattr__(self, "item_id", f"url-{digest}")

    def acquisition_request(self) -> AcquisitionRequest:
        return AcquisitionRequest(
            url=self.url,
            asset_id=self.asset_id,
            headers=dict(self.headers),
            timeout_s=self.timeout_s,
            max_bytes=self.max_bytes,
            render_mode=self.render_mode,
        )


@dataclass(frozen=True, slots=True)
class ExtractionJob:
    job_id: str
    items: tuple[JobItem, ...]
    fields: tuple[FieldSpec, ...]

    def __post_init__(self) -> None:
        if not self.job_id.strip():
            raise ValueError("job_id must not be blank")
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "fields", tuple(self.fields))
        if not self.items:
            raise ValueError("ExtractionJob must contain at least one item")
        if not self.fields:
            raise ValueError("ExtractionJob must contain at least one field")
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("ExtractionJob item_id values must be unique")
        field_keys = [spec.key for spec in self.fields]
        if len(field_keys) != len(set(field_keys)):
            raise ValueError("ExtractionJob field keys must be unique")

    @classmethod
    def from_urls(
        cls,
        job_id: str,
        urls: Sequence[str],
        fields: Sequence[FieldSpec],
    ) -> "ExtractionJob":
        return cls(
            job_id=job_id,
            items=tuple(JobItem(url=url) for url in urls),
            fields=tuple(fields),
        )

    @property
    def definition_hash(self) -> str:
        payload = {
            "items": [
                {
                    "item_id": item.item_id,
                    "url": item.url,
                    "asset_id": item.asset_id,
                    "headers": sorted(dict(item.headers).items()),
                    "timeout_s": item.timeout_s,
                    "max_bytes": item.max_bytes,
                    "render_mode": item.render_mode.value,
                }
                for item in self.items
            ],
            "fields": [
                {
                    "key": spec.key,
                    "required": spec.required,
                    "min_confidence": spec.min_confidence,
                    "min_margin": spec.min_margin,
                    "aliases": list(spec.aliases),
                }
                for spec in self.fields
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.5
    multiplier: float = 2.0
    max_delay_s: float = 10.0
    retryable_exceptions: tuple[type[Exception], ...] = (
        AcquisitionError,
        TimeoutError,
        OSError,
    )

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_s < 0 or self.max_delay_s < 0:
            raise ValueError("retry delays must be non-negative")
        if self.multiplier < 1.0:
            raise ValueError("multiplier must be >= 1.0")
        if not self.retryable_exceptions:
            raise ValueError("retryable_exceptions must not be empty")
        if any(
            not isinstance(exc_type, type) or not issubclass(exc_type, Exception)
            for exc_type in self.retryable_exceptions
        ):
            raise TypeError("retryable_exceptions must contain Exception types")

    def is_retryable(self, exc: Exception) -> bool:
        return isinstance(exc, self.retryable_exceptions)

    def delay_after(self, failed_attempt: int) -> float:
        if failed_attempt < 1:
            raise ValueError("failed_attempt must be >= 1")
        delay = self.base_delay_s * (self.multiplier ** (failed_attempt - 1))
        return min(self.max_delay_s, delay)


@dataclass(frozen=True, slots=True)
class JobError:
    error_type: str
    message: str
    retryable: bool

    @classmethod
    def from_exception(cls, exc: Exception, *, retryable: bool) -> "JobError":
        message = _URL_IN_ERROR_RE.sub("<url>", str(exc))
        if len(message) > MAX_ERROR_MESSAGE:
            message = message[: MAX_ERROR_MESSAGE - 3] + "..."
        return cls(type(exc).__name__, message, retryable)


@dataclass(frozen=True, slots=True)
class JobAttempt:
    attempt: int
    started_at: float
    finished_at: float
    success: bool
    retryable: bool = False
    error: JobError | None = None


@dataclass(frozen=True, slots=True)
class JobCheckpointItem:
    status: JobItemStatus = JobItemStatus.PENDING
    attempts: int = 0
    review_fields: tuple[str, ...] = ()
    unresolved_required_fields: tuple[str, ...] = ()
    last_error: JobError | None = None
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if self.attempts < 0:
            raise ValueError("checkpoint attempts must be non-negative")
        if not isinstance(self.status, JobItemStatus):
            object.__setattr__(self, "status", JobItemStatus(self.status))
        object.__setattr__(self, "review_fields", tuple(self.review_fields))
        object.__setattr__(
            self,
            "unresolved_required_fields",
            tuple(self.unresolved_required_fields),
        )


@dataclass(frozen=True, slots=True)
class JobCheckpoint:
    job_id: str
    definition_hash: str
    items: Mapping[str, JobCheckpointItem]
    revision: int = 0
    updated_at: float = 0.0
    version: int = JOB_CHECKPOINT_VERSION

    def __post_init__(self) -> None:
        if self.version != JOB_CHECKPOINT_VERSION:
            raise ValueError("Unsupported job checkpoint version")
        if self.revision < 0:
            raise ValueError("checkpoint revision must be non-negative")
        object.__setattr__(self, "items", dict(self.items))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "job_id": self.job_id,
            "definition_hash": self.definition_hash,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "items": {
                item_id: {
                    "status": state.status.value,
                    "attempts": state.attempts,
                    "review_fields": list(state.review_fields),
                    "unresolved_required_fields": list(state.unresolved_required_fields),
                    "last_error": None
                    if state.last_error is None
                    else {
                        "error_type": state.last_error.error_type,
                        "message": state.last_error.message,
                        "retryable": state.last_error.retryable,
                    },
                    "updated_at": state.updated_at,
                }
                for item_id, state in self.items.items()
            },
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "JobCheckpoint":
        if int(payload.get("version", -1)) != JOB_CHECKPOINT_VERSION:
            raise ValueError("Unsupported job checkpoint version")
        raw_items = payload.get("items", {})
        if not isinstance(raw_items, Mapping):
            raise ValueError("checkpoint items must be a mapping")
        items: dict[str, JobCheckpointItem] = {}
        for item_id, raw in raw_items.items():
            if not isinstance(raw, Mapping):
                raise ValueError("checkpoint item state must be a mapping")
            raw_error = raw.get("last_error")
            error = None
            if raw_error is not None:
                if not isinstance(raw_error, Mapping):
                    raise ValueError("checkpoint last_error must be a mapping")
                error = JobError(
                    error_type=str(raw_error.get("error_type", "Error")),
                    message=str(raw_error.get("message", "")),
                    retryable=bool(raw_error.get("retryable", False)),
                )
            items[str(item_id)] = JobCheckpointItem(
                status=JobItemStatus(str(raw.get("status", "pending"))),
                attempts=int(raw.get("attempts", 0)),
                review_fields=tuple(str(x) for x in raw.get("review_fields", ())),
                unresolved_required_fields=tuple(
                    str(x) for x in raw.get("unresolved_required_fields", ())
                ),
                last_error=error,
                updated_at=float(raw.get("updated_at", 0.0)),
            )
        return cls(
            version=int(payload["version"]),
            job_id=str(payload["job_id"]),
            definition_hash=str(payload["definition_hash"]),
            revision=int(payload.get("revision", 0)),
            updated_at=float(payload.get("updated_at", 0.0)),
            items=items,
        )


@dataclass(frozen=True, slots=True)
class JobItemResult:
    item: JobItem
    status: JobItemStatus
    attempt_count: int
    attempts: tuple[JobAttempt, ...] = ()
    result: URLExtractionResult | None = None
    error: JobError | None = None
    review_fields: tuple[str, ...] = ()
    unresolved_required_fields: tuple[str, ...] = ()
    resumed: bool = False


@dataclass(frozen=True, slots=True)
class JobRunResult:
    job: ExtractionJob
    items: tuple[JobItemResult, ...]
    started_at: float
    finished_at: float
    checkpoint_revision: int

    @property
    def complete(self) -> bool:
        return all(item.status.terminal for item in self.items)

    @property
    def status(self) -> JobStatus:
        if not self.complete:
            return JobStatus.PARTIAL
        if any(item.status is JobItemStatus.FAILED for item in self.items):
            return JobStatus.COMPLETED_WITH_FAILURES
        if any(
            item.status in {JobItemStatus.REVIEW_REQUIRED, JobItemStatus.INCOMPLETE}
            for item in self.items
        ):
            return JobStatus.NEEDS_ATTENTION
        return JobStatus.SUCCEEDED

    @property
    def succeeded(self) -> int:
        return sum(item.status is JobItemStatus.SUCCEEDED for item in self.items)

    @property
    def failed(self) -> int:
        return sum(item.status is JobItemStatus.FAILED for item in self.items)

    @property
    def review_required(self) -> int:
        return sum(item.status is JobItemStatus.REVIEW_REQUIRED for item in self.items)

    @property
    def incomplete(self) -> int:
        return sum(item.status is JobItemStatus.INCOMPLETE for item in self.items)


class JobCheckpointMismatchError(RuntimeError):
    """Raised when a durable checkpoint belongs to a different job definition."""
