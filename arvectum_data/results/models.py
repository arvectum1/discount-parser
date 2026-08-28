from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from ..orchestration import URLExtractionResult


RESULT_SCHEMA_VERSION = 1


class StoredResultStatus(StrEnum):
    READY = "ready"
    REVIEW_REQUIRED = "review_required"
    INCOMPLETE = "incomplete"


class ResultPersistenceError(RuntimeError):
    pass


class ResultSerializationError(ResultPersistenceError):
    pass


class ResultIntegrityError(ResultPersistenceError):
    pass


class ResultConflictError(ResultPersistenceError):
    pass


class ResultDefinitionMismatchError(ResultPersistenceError):
    pass


class ResultNotFoundError(ResultPersistenceError):
    pass


def result_status(result: URLExtractionResult) -> StoredResultStatus:
    if result.requires_confirmation:
        return StoredResultStatus.REVIEW_REQUIRED
    if result.unresolved_required_fields:
        return StoredResultStatus.INCOMPLETE
    return StoredResultStatus.READY


def payload_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredResultRecord:
    job_id: str
    item_id: str
    definition_hash: str
    status: StoredResultStatus
    payload: Mapping[str, Any]
    payload_hash: str
    revision: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    version: int = RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.version != RESULT_SCHEMA_VERSION:
            raise ValueError("Unsupported durable result schema version")
        if not self.job_id.strip() or not self.item_id.strip():
            raise ValueError("job_id and item_id must not be blank")
        if not self.definition_hash.strip():
            raise ValueError("definition_hash must not be blank")
        if not isinstance(self.status, StoredResultStatus):
            object.__setattr__(self, "status", StoredResultStatus(self.status))
        if self.revision < 0:
            raise ValueError("result revision must be non-negative")
        copied = json.loads(
            json.dumps(
                self.payload,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
        )
        object.__setattr__(self, "payload", copied)
        actual = payload_hash(copied)
        if self.payload_hash != actual:
            raise ResultIntegrityError("Durable result payload hash mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "job_id": self.job_id,
            "item_id": self.item_id,
            "definition_hash": self.definition_hash,
            "status": self.status.value,
            "payload": self.payload,
            "payload_hash": self.payload_hash,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StoredResultRecord":
        if int(data.get("version", -1)) != RESULT_SCHEMA_VERSION:
            raise ValueError("Unsupported durable result schema version")
        payload = data.get("payload")
        if not isinstance(payload, Mapping):
            raise ResultIntegrityError("Durable result payload must be a mapping")
        return cls(
            version=int(data["version"]),
            job_id=str(data["job_id"]),
            item_id=str(data["item_id"]),
            definition_hash=str(data["definition_hash"]),
            status=StoredResultStatus(str(data["status"])),
            payload=dict(payload),
            payload_hash=str(data["payload_hash"]),
            revision=int(data.get("revision", 0)),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
        )
