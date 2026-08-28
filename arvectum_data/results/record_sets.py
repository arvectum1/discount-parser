from __future__ import annotations

import base64
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..engine import (
    MultiRecordExtractionEngine,
    RawAsset,
    RecordBoundary,
    RecordBoundaryStatus,
    RecordExtractionResult,
    RecordSetResult,
    RecordStatus,
)
from .codec import ResultCodec
from .models import (
    ResultConflictError,
    ResultDefinitionMismatchError,
    ResultIntegrityError,
    ResultNotFoundError,
    StoredResultRecord,
    StoredResultStatus,
    payload_hash,
)
from .stores import ResultStore


MULTI_RECORD_RESULT_SCHEMA_VERSION = 1
_RECORD_PREFIX = "__dp_record_v1__:"
_SET_PREFIX = "__dp_record_set_v1__:"


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")


def record_storage_item_id(item_id: str, record_id: str) -> str:
    if not item_id.strip() or not record_id.strip():
        raise ValueError("item_id and record_id must not be blank")
    return f"{_RECORD_PREFIX}{_b64(item_id)}:{_b64(record_id)}"


def record_set_storage_item_id(item_id: str) -> str:
    if not item_id.strip():
        raise ValueError("item_id must not be blank")
    return f"{_SET_PREFIX}{_b64(item_id)}"


def parse_record_storage_item_id(storage_item_id: str) -> tuple[str, str] | None:
    if not storage_item_id.startswith(_RECORD_PREFIX):
        return None
    encoded = storage_item_id[len(_RECORD_PREFIX) :]
    parts = encoded.split(":", 1)
    if len(parts) != 2:
        raise ResultIntegrityError("Malformed durable record storage item id")
    try:
        return _unb64(parts[0]), _unb64(parts[1])
    except Exception as exc:
        raise ResultIntegrityError("Malformed durable record storage identity") from exc


def _storage_status(status: RecordStatus) -> StoredResultStatus:
    if status is RecordStatus.NEEDS_CONFIRMATION:
        return StoredResultStatus.REVIEW_REQUIRED
    if status is RecordStatus.READY:
        return StoredResultStatus.READY
    return StoredResultStatus.INCOMPLETE


@dataclass(frozen=True, slots=True)
class StoredRecordResult:
    """Public durable identity for one independently revisioned extracted record."""

    job_id: str
    item_id: str
    record_id: str
    definition_hash: str
    status: RecordStatus
    revision: int
    created_at: float
    updated_at: float
    storage_record: StoredResultRecord

    def __post_init__(self) -> None:
        parsed = parse_record_storage_item_id(self.storage_record.item_id)
        if parsed != (self.item_id, self.record_id):
            raise ResultIntegrityError("Durable record storage identity mismatch")
        if self.storage_record.job_id != self.job_id:
            raise ResultIntegrityError("Durable record job identity mismatch")
        if self.storage_record.definition_hash != self.definition_hash:
            raise ResultIntegrityError("Durable record definition hash mismatch")
        if self.storage_record.revision != self.revision:
            raise ResultIntegrityError("Durable record revision mismatch")


@dataclass(frozen=True, slots=True)
class RecordSetManifest:
    job_id: str
    item_id: str
    definition_hash: str
    asset: RawAsset
    record_ids: tuple[str, ...]
    record_provider_errors: Mapping[str, str]
    record_provider_warnings: Mapping[str, tuple[str, ...]]
    revision: int
    storage_record: StoredResultRecord


class RecordResultCodec:
    """Strict codec for record boundaries and their independent field decisions."""

    def __init__(self, *, include_raw_content: bool = False) -> None:
        self.include_raw_content = include_raw_content
        self._base = ResultCodec(include_raw_content=include_raw_content)

    def encode_record(self, result: RecordExtractionResult) -> dict[str, Any]:
        return {
            "multi_record_version": MULTI_RECORD_RESULT_SCHEMA_VERSION,
            "kind": "record",
            "raw_content_persisted": self.include_raw_content,
            "record_id": result.record_id,
            "boundary_status": result.boundary_status.value,
            "boundary_reason": result.boundary_reason,
            "boundary": {
                "provider": result.boundary.provider,
                "source_ref": result.boundary.source_ref,
                "ordinal": result.boundary.ordinal,
                "confidence": result.boundary.confidence,
                "asset": self._base._encode_asset(result.boundary.asset),
                "evidence": [
                    self._base._encode_evidence(item) for item in result.boundary.evidence
                ],
                "metadata": self._base._encode_value(dict(result.boundary.metadata)),
            },
            "extraction": {
                "decisions": {
                    key: self._base._encode_decision(decision)
                    for key, decision in result.extraction.decisions.items()
                },
                "provider_errors": dict(result.extraction.provider_errors),
            },
        }

    def decode_record(self, payload: Mapping[str, Any]) -> RecordExtractionResult:
        self._validate_header(payload, "record")
        raw_flag = payload.get("raw_content_persisted", False)
        if not isinstance(raw_flag, bool):
            raise ResultIntegrityError("raw_content_persisted must be boolean")
        boundary_raw = payload.get("boundary")
        extraction_raw = payload.get("extraction")
        if not isinstance(boundary_raw, Mapping) or not isinstance(extraction_raw, Mapping):
            raise ResultIntegrityError("Durable record boundary/extraction must be mappings")
        asset_raw = boundary_raw.get("asset")
        if not isinstance(asset_raw, Mapping):
            raise ResultIntegrityError("Durable record boundary asset must be a mapping")
        asset = self._base._decode_asset(
            asset_raw,
            raw_content_persisted=raw_flag,
        )
        evidence_raw = boundary_raw.get("evidence", ())
        if not isinstance(evidence_raw, Sequence) or isinstance(evidence_raw, (str, bytes)):
            raise ResultIntegrityError("Durable record boundary evidence must be a sequence")
        metadata = self._base._decode_value(
            boundary_raw.get("metadata", ["dict", []])
        )
        if not isinstance(metadata, Mapping):
            raise ResultIntegrityError("Durable record boundary metadata must decode to mapping")
        record_id = str(payload["record_id"])
        boundary = RecordBoundary(
            record_id=record_id,
            asset=asset,
            provider=str(boundary_raw["provider"]),
            source_ref=str(boundary_raw["source_ref"]),
            ordinal=int(boundary_raw["ordinal"]),
            confidence=float(boundary_raw["confidence"]),
            evidence=tuple(self._base._decode_evidence(item) for item in evidence_raw),
            metadata=dict(metadata),
        )
        decisions_raw = extraction_raw.get("decisions", {})
        if not isinstance(decisions_raw, Mapping):
            raise ResultIntegrityError("Durable record decisions must be a mapping")
        from ..engine import ExtractionResult

        extraction = ExtractionResult(
            asset=asset,
            decisions={
                str(key): self._base._decode_decision(value)
                for key, value in decisions_raw.items()
            },
            provider_errors={
                str(key): str(value)
                for key, value in dict(extraction_raw.get("provider_errors", {})).items()
            },
        )
        return RecordExtractionResult(
            boundary=boundary,
            boundary_status=RecordBoundaryStatus(str(payload["boundary_status"])),
            extraction=extraction,
            boundary_reason=(
                None
                if payload.get("boundary_reason") is None
                else str(payload.get("boundary_reason"))
            ),
        )

    def encode_manifest(self, result: RecordSetResult) -> dict[str, Any]:
        return {
            "multi_record_version": MULTI_RECORD_RESULT_SCHEMA_VERSION,
            "kind": "record_set",
            "raw_content_persisted": self.include_raw_content,
            "asset": self._base._encode_asset(result.asset),
            "record_ids": [record.record_id for record in result.records],
            "record_provider_errors": dict(result.record_provider_errors),
            "record_provider_warnings": {
                str(key): list(value)
                for key, value in result.record_provider_warnings.items()
            },
        }

    def decode_manifest_payload(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[RawAsset, tuple[str, ...], dict[str, str], dict[str, tuple[str, ...]]]:
        self._validate_header(payload, "record_set")
        raw_flag = payload.get("raw_content_persisted", False)
        if not isinstance(raw_flag, bool):
            raise ResultIntegrityError("raw_content_persisted must be boolean")
        asset_raw = payload.get("asset")
        if not isinstance(asset_raw, Mapping):
            raise ResultIntegrityError("Durable record-set asset must be a mapping")
        asset = self._base._decode_asset(
            asset_raw,
            raw_content_persisted=raw_flag,
        )
        ids_raw = payload.get("record_ids", ())
        if not isinstance(ids_raw, Sequence) or isinstance(ids_raw, (str, bytes)):
            raise ResultIntegrityError("record_ids must be a sequence")
        record_ids = tuple(str(item) for item in ids_raw)
        if len(record_ids) != len(set(record_ids)):
            raise ResultIntegrityError("record_ids must be unique")
        errors_raw = payload.get("record_provider_errors", {})
        warnings_raw = payload.get("record_provider_warnings", {})
        if not isinstance(errors_raw, Mapping) or not isinstance(warnings_raw, Mapping):
            raise ResultIntegrityError("provider errors/warnings must be mappings")
        warnings: dict[str, tuple[str, ...]] = {}
        for key, value in warnings_raw.items():
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise ResultIntegrityError("provider warning list must be a sequence")
            warnings[str(key)] = tuple(str(item) for item in value)
        return (
            asset,
            record_ids,
            {str(key): str(value) for key, value in errors_raw.items()},
            warnings,
        )

    @staticmethod
    def _validate_header(payload: Mapping[str, Any], kind: str) -> None:
        if int(payload.get("multi_record_version", -1)) != MULTI_RECORD_RESULT_SCHEMA_VERSION:
            raise ResultIntegrityError("Unsupported durable multi-record result version")
        if payload.get("kind") != kind:
            raise ResultIntegrityError(f"Expected durable multi-record payload kind {kind!r}")


class RecordResultRepository:
    """Persist a record set as one immutable manifest plus independently revisioned records.

    Existing DP-008 ResultStore backends are reused without changing their schema. A
    reserved, reversible item-id namespace keeps multi-record rows separate from
    ordinary single-result rows. Each record therefore has its own optimistic
    revision and can be reviewed concurrently with sibling records.
    """

    def __init__(
        self,
        store: ResultStore,
        *,
        codec: RecordResultCodec | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.store = store
        self.codec = codec or RecordResultCodec()
        self._clock = clock or time.time

    def persist_set(
        self,
        *,
        job_id: str,
        item_id: str,
        definition_hash: str,
        result: RecordSetResult,
    ) -> tuple[RecordSetManifest, tuple[StoredRecordResult, ...]]:
        manifest = self._persist_manifest(
            job_id=job_id,
            item_id=item_id,
            definition_hash=definition_hash,
            result=result,
        )
        stored = tuple(
            self.persist_record(
                job_id=job_id,
                item_id=item_id,
                definition_hash=definition_hash,
                result=record,
            )
            for record in result.records
        )
        return manifest, stored

    def persist_record(
        self,
        *,
        job_id: str,
        item_id: str,
        definition_hash: str,
        result: RecordExtractionResult,
    ) -> StoredRecordResult:
        payload = self.codec.encode_record(result)
        storage_item_id = record_storage_item_id(item_id, result.record_id)
        existing = self.store.load(job_id, storage_item_id)
        digest = payload_hash(payload)
        if existing is not None:
            self._verify_definition(existing, definition_hash)
            if existing.payload_hash == digest:
                return self._wrap(existing, item_id, result.record_id, result.status)
            raise ResultConflictError(
                "Existing durable record differs; explicit review/update/reset is required"
            )
        now = self._clock()
        record = StoredResultRecord(
            job_id=job_id,
            item_id=storage_item_id,
            definition_hash=definition_hash,
            status=_storage_status(result.status),
            payload=payload,
            payload_hash=digest,
            created_at=now,
            updated_at=now,
        )
        created = self.store.create(record)
        return self._wrap(created, item_id, result.record_id, result.status)

    def load_record(
        self,
        job_id: str,
        item_id: str,
        record_id: str,
        *,
        expected_definition_hash: str | None = None,
    ) -> StoredRecordResult | None:
        storage = self.store.load(job_id, record_storage_item_id(item_id, record_id))
        if storage is None:
            return None
        if expected_definition_hash is not None:
            self._verify_definition(storage, expected_definition_hash)
        result = self.codec.decode_record(storage.payload)
        if result.record_id != record_id:
            raise ResultIntegrityError("Durable record payload identity mismatch")
        return self._wrap(storage, item_id, record_id, result.status)

    def load_result(
        self,
        job_id: str,
        item_id: str,
        record_id: str,
        *,
        expected_definition_hash: str | None = None,
    ) -> tuple[StoredRecordResult, RecordExtractionResult] | None:
        stored = self.load_record(
            job_id,
            item_id,
            record_id,
            expected_definition_hash=expected_definition_hash,
        )
        if stored is None:
            return None
        return stored, self.codec.decode_record(stored.storage_record.payload)

    def update_result(
        self,
        stored: StoredRecordResult,
        result: RecordExtractionResult,
        *,
        expected_revision: int | None = None,
    ) -> StoredRecordResult:
        if result.record_id != stored.record_id:
            raise ResultConflictError("Cannot change durable record identity")
        expected = stored.revision if expected_revision is None else expected_revision
        if expected != stored.revision:
            raise ResultConflictError("Expected revision does not match loaded durable record")
        preserve_raw = bool(stored.storage_record.payload.get("raw_content_persisted", False))
        codec = (
            self.codec
            if self.codec.include_raw_content is preserve_raw
            else RecordResultCodec(include_raw_content=preserve_raw)
        )
        payload = codec.encode_record(result)
        candidate = replace(
            stored.storage_record,
            status=_storage_status(result.status),
            payload=payload,
            payload_hash=payload_hash(payload),
            updated_at=self._clock(),
        )
        updated = self.store.update(candidate, expected_revision=expected)
        return self._wrap(updated, stored.item_id, stored.record_id, result.status)

    def pending_reviews(
        self,
        *,
        job_id: str | None = None,
        item_id: str | None = None,
    ) -> tuple[StoredRecordResult, ...]:
        result: list[StoredRecordResult] = []
        for storage in self.store.list(
            job_id=job_id,
            status=StoredResultStatus.REVIEW_REQUIRED,
        ):
            identity = parse_record_storage_item_id(storage.item_id)
            if identity is None:
                continue
            parent_item_id, record_id = identity
            if item_id is not None and parent_item_id != item_id:
                continue
            decoded = self.codec.decode_record(storage.payload)
            if decoded.status is not RecordStatus.NEEDS_CONFIRMATION:
                raise ResultIntegrityError(
                    "Durable record storage status disagrees with record payload status"
                )
            result.append(self._wrap(storage, parent_item_id, record_id, decoded.status))
        return tuple(sorted(result, key=lambda item: (item.job_id, item.item_id, item.record_id)))

    def load_manifest(
        self,
        job_id: str,
        item_id: str,
        *,
        expected_definition_hash: str | None = None,
    ) -> RecordSetManifest | None:
        storage = self.store.load(job_id, record_set_storage_item_id(item_id))
        if storage is None:
            return None
        if expected_definition_hash is not None:
            self._verify_definition(storage, expected_definition_hash)
        asset, record_ids, errors, warnings = self.codec.decode_manifest_payload(storage.payload)
        return RecordSetManifest(
            job_id=job_id,
            item_id=item_id,
            definition_hash=storage.definition_hash,
            asset=asset,
            record_ids=record_ids,
            record_provider_errors=errors,
            record_provider_warnings=warnings,
            revision=storage.revision,
            storage_record=storage,
        )

    def load_set(
        self,
        job_id: str,
        item_id: str,
        *,
        expected_definition_hash: str | None = None,
    ) -> RecordSetResult | None:
        manifest = self.load_manifest(
            job_id,
            item_id,
            expected_definition_hash=expected_definition_hash,
        )
        if manifest is None:
            return None
        records: list[RecordExtractionResult] = []
        for record_id in manifest.record_ids:
            loaded = self.load_result(
                job_id,
                item_id,
                record_id,
                expected_definition_hash=manifest.definition_hash,
            )
            if loaded is None:
                raise ResultIntegrityError(
                    f"Durable record-set manifest references missing record {record_id!r}"
                )
            records.append(loaded[1])
        return RecordSetResult(
            asset=manifest.asset,
            records=tuple(records),
            record_provider_errors=manifest.record_provider_errors,
            record_provider_warnings=manifest.record_provider_warnings,
        )

    def clear_item(self, job_id: str, item_id: str) -> None:
        self.store.delete(job_id, record_set_storage_item_id(item_id))
        for storage in tuple(self.store.list(job_id=job_id)):
            identity = parse_record_storage_item_id(storage.item_id)
            if identity is not None and identity[0] == item_id:
                self.store.delete(job_id, storage.item_id)

    def clear_job(self, job_id: str) -> None:
        for storage in tuple(self.store.list(job_id=job_id)):
            if storage.item_id.startswith(_RECORD_PREFIX) or storage.item_id.startswith(_SET_PREFIX):
                self.store.delete(job_id, storage.item_id)

    def _persist_manifest(
        self,
        *,
        job_id: str,
        item_id: str,
        definition_hash: str,
        result: RecordSetResult,
    ) -> RecordSetManifest:
        payload = self.codec.encode_manifest(result)
        storage_item_id = record_set_storage_item_id(item_id)
        existing = self.store.load(job_id, storage_item_id)
        digest = payload_hash(payload)
        if existing is not None:
            self._verify_definition(existing, definition_hash)
            if existing.payload_hash != digest:
                raise ResultConflictError(
                    "Existing durable record-set manifest differs; explicit reset is required"
                )
            manifest = self.load_manifest(job_id, item_id)
            if manifest is None:  # pragma: no cover - defensive
                raise ResultIntegrityError("Durable record-set manifest disappeared")
            return manifest
        now = self._clock()
        storage = self.store.create(
            StoredResultRecord(
                job_id=job_id,
                item_id=storage_item_id,
                definition_hash=definition_hash,
                status=StoredResultStatus.READY,
                payload=payload,
                payload_hash=digest,
                created_at=now,
                updated_at=now,
            )
        )
        asset, record_ids, errors, warnings = self.codec.decode_manifest_payload(storage.payload)
        return RecordSetManifest(
            job_id=job_id,
            item_id=item_id,
            definition_hash=definition_hash,
            asset=asset,
            record_ids=record_ids,
            record_provider_errors=errors,
            record_provider_warnings=warnings,
            revision=storage.revision,
            storage_record=storage,
        )

    @staticmethod
    def _verify_definition(record: StoredResultRecord, definition_hash: str) -> None:
        if record.definition_hash != definition_hash:
            raise ResultDefinitionMismatchError(
                "Durable record result belongs to another job definition"
            )

    @staticmethod
    def _wrap(
        storage: StoredResultRecord,
        item_id: str,
        record_id: str,
        status: RecordStatus,
    ) -> StoredRecordResult:
        return StoredRecordResult(
            job_id=storage.job_id,
            item_id=item_id,
            record_id=record_id,
            definition_hash=storage.definition_hash,
            status=status,
            revision=storage.revision,
            created_at=storage.created_at,
            updated_at=storage.updated_at,
            storage_record=storage,
        )


@dataclass(frozen=True, slots=True)
class RecordReviewUpdate:
    record: StoredRecordResult
    result: RecordExtractionResult


class DurableRecordReviewCoordinator:
    """Continue record-scoped review after restart without reacquiring a source URL."""

    def __init__(
        self,
        result_store: ResultStore,
        *,
        repository: RecordResultRepository | None = None,
        review_engine: MultiRecordExtractionEngine | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.repository = repository or RecordResultRepository(
            result_store,
            clock=clock,
        )
        self.review_engine = review_engine or MultiRecordExtractionEngine((), ())

    def pending(
        self,
        *,
        job_id: str | None = None,
        item_id: str | None = None,
    ) -> tuple[StoredRecordResult, ...]:
        return self.repository.pending_reviews(job_id=job_id, item_id=item_id)

    def get(
        self,
        job_id: str,
        item_id: str,
        record_id: str,
        *,
        expected_definition_hash: str | None = None,
    ) -> tuple[StoredRecordResult, RecordExtractionResult]:
        loaded = self.repository.load_result(
            job_id,
            item_id,
            record_id,
            expected_definition_hash=expected_definition_hash,
        )
        if loaded is None:
            raise ResultNotFoundError("Durable record result does not exist")
        return loaded

    def confirm_boundary(
        self,
        job_id: str,
        item_id: str,
        record_id: str,
        *,
        accept: bool,
        expected_revision: int | None = None,
        expected_definition_hash: str | None = None,
    ) -> RecordReviewUpdate:
        stored, result = self.get(
            job_id,
            item_id,
            record_id,
            expected_definition_hash=expected_definition_hash,
        )
        self._check_reviewable(stored, expected_revision)
        reviewed = self._single_set_apply(
            result,
            lambda record_set: self.review_engine.confirm_boundary(
                record_set,
                record_id,
                accept=accept,
            ),
        )
        changed = reviewed.record(record_id)
        updated = self.repository.update_result(
            stored,
            changed,
            expected_revision=stored.revision,
        )
        return RecordReviewUpdate(updated, changed)

    def confirm_fields(
        self,
        job_id: str,
        item_id: str,
        record_id: str,
        selections: Mapping[str, str | None],
        *,
        expected_revision: int | None = None,
        expected_definition_hash: str | None = None,
    ) -> RecordReviewUpdate:
        if not selections:
            raise ValueError("selections must not be empty")
        stored, result = self.get(
            job_id,
            item_id,
            record_id,
            expected_definition_hash=expected_definition_hash,
        )
        self._check_reviewable(stored, expected_revision)
        reviewed = self._single_set_apply(
            result,
            lambda record_set: self.review_engine.confirm_fields(
                record_set,
                record_id,
                selections,
            ),
        )
        changed = reviewed.record(record_id)
        updated = self.repository.update_result(
            stored,
            changed,
            expected_revision=stored.revision,
        )
        return RecordReviewUpdate(updated, changed)

    @staticmethod
    def _single_set_apply(
        result: RecordExtractionResult,
        operation: Callable[[RecordSetResult], RecordSetResult],
    ) -> RecordSetResult:
        wrapper = RecordSetResult(asset=result.boundary.asset, records=(result,))
        return operation(wrapper)

    @staticmethod
    def _check_reviewable(
        stored: StoredRecordResult,
        expected_revision: int | None,
    ) -> None:
        if stored.status is not RecordStatus.NEEDS_CONFIRMATION:
            raise ValueError(
                f"Durable record is {stored.status.value}; only review-required records may be reviewed"
            )
        if expected_revision is not None and stored.revision != expected_revision:
            raise ResultConflictError("Durable record review revision conflict")
