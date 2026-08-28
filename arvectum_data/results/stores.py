from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from ..orchestration import URLExtractionResult
from .codec import ResultCodec
from .models import (
    ResultConflictError,
    ResultDefinitionMismatchError,
    ResultNotFoundError,
    StoredResultRecord,
    StoredResultStatus,
    payload_hash,
    result_status,
)


class ResultStore(Protocol):
    def load(self, job_id: str, item_id: str) -> StoredResultRecord | None: ...
    def create(self, record: StoredResultRecord) -> StoredResultRecord: ...
    def update(
        self,
        record: StoredResultRecord,
        *,
        expected_revision: int,
    ) -> StoredResultRecord: ...
    def list(
        self,
        *,
        job_id: str | None = None,
        status: StoredResultStatus | None = None,
    ) -> Sequence[StoredResultRecord]: ...
    def delete(self, job_id: str, item_id: str) -> None: ...
    def clear_job(self, job_id: str) -> None: ...


class InMemoryResultStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], dict] = {}

    def load(self, job_id: str, item_id: str) -> StoredResultRecord | None:
        payload = self._records.get((job_id, item_id))
        if payload is None:
            return None
        return StoredResultRecord.from_dict(json.loads(json.dumps(payload, ensure_ascii=False)))

    def create(self, record: StoredResultRecord) -> StoredResultRecord:
        key = (record.job_id, record.item_id)
        if key in self._records:
            raise ResultConflictError("Durable result already exists")
        stored = replace(record, revision=1)
        self._records[key] = stored.to_dict()
        return self.load(*key)  # type: ignore[return-value]

    def update(
        self,
        record: StoredResultRecord,
        *,
        expected_revision: int,
    ) -> StoredResultRecord:
        key = (record.job_id, record.item_id)
        current = self.load(*key)
        if current is None:
            raise ResultNotFoundError("Durable result does not exist")
        if current.revision != expected_revision:
            raise ResultConflictError("Durable result revision conflict")
        if current.definition_hash != record.definition_hash:
            raise ResultDefinitionMismatchError("Durable result definition hash changed")
        stored = replace(
            record,
            revision=current.revision + 1,
            created_at=current.created_at,
        )
        self._records[key] = stored.to_dict()
        return self.load(*key)  # type: ignore[return-value]

    def list(
        self,
        *,
        job_id: str | None = None,
        status: StoredResultStatus | None = None,
    ) -> tuple[StoredResultRecord, ...]:
        records = [StoredResultRecord.from_dict(item) for item in self._records.values()]
        if job_id is not None:
            records = [item for item in records if item.job_id == job_id]
        if status is not None:
            records = [item for item in records if item.status is status]
        return tuple(sorted(records, key=lambda item: (item.job_id, item.item_id)))

    def delete(self, job_id: str, item_id: str) -> None:
        self._records.pop((job_id, item_id), None)

    def clear_job(self, job_id: str) -> None:
        for key in [key for key in self._records if key[0] == job_id]:
            del self._records[key]


class JsonResultStore:
    """Atomic local durable result store. Intended for a single writer process."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.directory = Path(directory)

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

    def _job_dir(self, job_id: str) -> Path:
        return self.directory / f"job-{self._digest(job_id)}"

    def _path(self, job_id: str, item_id: str) -> Path:
        return self._job_dir(job_id) / f"item-{self._digest(item_id)}.json"

    def load(self, job_id: str, item_id: str) -> StoredResultRecord | None:
        path = self._path(job_id, item_id)
        if not path.exists():
            return None
        record = StoredResultRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if record.job_id != job_id or record.item_id != item_id:
            raise ResultConflictError("Durable result path identity mismatch")
        return record

    def create(self, record: StoredResultRecord) -> StoredResultRecord:
        if self.load(record.job_id, record.item_id) is not None:
            raise ResultConflictError("Durable result already exists")
        stored = replace(record, revision=1)
        self._persist(stored)
        return stored

    def update(
        self,
        record: StoredResultRecord,
        *,
        expected_revision: int,
    ) -> StoredResultRecord:
        current = self.load(record.job_id, record.item_id)
        if current is None:
            raise ResultNotFoundError("Durable result does not exist")
        if current.revision != expected_revision:
            raise ResultConflictError("Durable result revision conflict")
        if current.definition_hash != record.definition_hash:
            raise ResultDefinitionMismatchError("Durable result definition hash changed")
        stored = replace(
            record,
            revision=current.revision + 1,
            created_at=current.created_at,
        )
        self._persist(stored)
        return stored

    def list(
        self,
        *,
        job_id: str | None = None,
        status: StoredResultStatus | None = None,
    ) -> tuple[StoredResultRecord, ...]:
        roots = [self._job_dir(job_id)] if job_id is not None else list(self.directory.glob("job-*"))
        records: list[StoredResultRecord] = []
        for root in roots:
            if not root.exists():
                continue
            for path in root.glob("item-*.json"):
                record = StoredResultRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
                if job_id is not None and record.job_id != job_id:
                    continue
                if status is not None and record.status is not status:
                    continue
                records.append(record)
        return tuple(sorted(records, key=lambda item: (item.job_id, item.item_id)))

    def delete(self, job_id: str, item_id: str) -> None:
        try:
            self._path(job_id, item_id).unlink()
        except FileNotFoundError:
            pass

    def clear_job(self, job_id: str) -> None:
        root = self._job_dir(job_id)
        if not root.exists():
            return
        for path in root.glob("item-*.json"):
            path.unlink()
        try:
            root.rmdir()
        except OSError:
            pass

    def _persist(self, record: StoredResultRecord) -> None:
        root = self._job_dir(record.job_id)
        root.mkdir(parents=True, exist_ok=True)
        path = self._path(record.job_id, record.item_id)
        payload = json.dumps(
            record.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=root,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


class SQLiteResultStore:
    """WAL-backed multi-process durable result store for one runtime node."""

    def __init__(self, path: str | os.PathLike[str], *, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(path),
            timeout=busy_timeout_ms / 1000.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS durable_results (
                    job_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    definition_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY (job_id, item_id)
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_durable_results_status ON durable_results(status, job_id)"
            )

    def load(self, job_id: str, item_id: str) -> StoredResultRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT record_json FROM durable_results WHERE job_id=? AND item_id=?",
                (job_id, item_id),
            ).fetchone()
        if row is None:
            return None
        record = StoredResultRecord.from_dict(json.loads(row["record_json"]))
        if record.job_id != job_id or record.item_id != item_id:
            raise ResultConflictError("SQLite durable result identity mismatch")
        return record

    def create(self, record: StoredResultRecord) -> StoredResultRecord:
        stored = replace(record, revision=1)
        serialized = self._serialize(stored)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    INSERT INTO durable_results(job_id,item_id,definition_hash,status,revision,record_json)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (
                        stored.job_id,
                        stored.item_id,
                        stored.definition_hash,
                        stored.status.value,
                        stored.revision,
                        serialized,
                    ),
                )
                self._connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._rollback()
                raise ResultConflictError("Durable result already exists") from exc
            except Exception:
                self._rollback()
                raise
        return stored

    def update(
        self,
        record: StoredResultRecord,
        *,
        expected_revision: int,
    ) -> StoredResultRecord:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT revision,definition_hash,record_json FROM durable_results WHERE job_id=? AND item_id=?",
                    (record.job_id, record.item_id),
                ).fetchone()
                if row is None:
                    raise ResultNotFoundError("Durable result does not exist")
                if int(row["revision"]) != expected_revision:
                    raise ResultConflictError("Durable result revision conflict")
                if str(row["definition_hash"]) != record.definition_hash:
                    raise ResultDefinitionMismatchError("Durable result definition hash changed")
                current = StoredResultRecord.from_dict(json.loads(row["record_json"]))
                stored = replace(
                    record,
                    revision=expected_revision + 1,
                    created_at=current.created_at,
                )
                self._connection.execute(
                    """
                    UPDATE durable_results
                    SET status=?,revision=?,record_json=?
                    WHERE job_id=? AND item_id=?
                    """,
                    (
                        stored.status.value,
                        stored.revision,
                        self._serialize(stored),
                        stored.job_id,
                        stored.item_id,
                    ),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._rollback()
                raise
        return stored

    def list(
        self,
        *,
        job_id: str | None = None,
        status: StoredResultStatus | None = None,
    ) -> tuple[StoredResultRecord, ...]:
        where: list[str] = []
        params: list[str] = []
        if job_id is not None:
            where.append("job_id=?")
            params.append(job_id)
        if status is not None:
            where.append("status=?")
            params.append(status.value)
        sql = "SELECT record_json FROM durable_results"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY job_id,item_id"
        with self._lock:
            rows = self._connection.execute(sql, tuple(params)).fetchall()
        return tuple(StoredResultRecord.from_dict(json.loads(row["record_json"])) for row in rows)

    def delete(self, job_id: str, item_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM durable_results WHERE job_id=? AND item_id=?",
                (job_id, item_id),
            )

    def clear_job(self, job_id: str) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM durable_results WHERE job_id=?", (job_id,))

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteResultStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def _serialize(record: StoredResultRecord) -> str:
        return json.dumps(
            record.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


class ResultRepository:
    """Codec + optimistic-revision facade used by workers and review coordinators."""

    def __init__(
        self,
        store: ResultStore,
        *,
        codec: ResultCodec | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.store = store
        self.codec = codec or ResultCodec()
        self._clock = clock or time.time

    def persist_initial(
        self,
        *,
        job_id: str,
        item_id: str,
        definition_hash: str,
        result: URLExtractionResult,
    ) -> StoredResultRecord:
        payload = self.codec.encode(result)
        digest = payload_hash(payload)
        existing = self.store.load(job_id, item_id)
        if existing is not None:
            if existing.definition_hash != definition_hash:
                raise ResultDefinitionMismatchError(
                    "Existing durable result belongs to another job definition"
                )
            if existing.payload_hash == digest:
                return existing
            raise ResultConflictError(
                "Existing durable result differs; explicit review/update/reset is required"
            )
        now = self._clock()
        record = StoredResultRecord(
            job_id=job_id,
            item_id=item_id,
            definition_hash=definition_hash,
            status=result_status(result),
            payload=payload,
            payload_hash=digest,
            created_at=now,
            updated_at=now,
        )
        return self.store.create(record)

    def update_result(
        self,
        record: StoredResultRecord,
        result: URLExtractionResult,
        *,
        expected_revision: int | None = None,
    ) -> StoredResultRecord:
        expected = record.revision if expected_revision is None else expected_revision
        if expected != record.revision:
            raise ResultConflictError("Expected revision does not match loaded durable result")
        preserve_raw = bool(record.payload.get("raw_content_persisted", False))
        codec = (
            self.codec
            if self.codec.include_raw_content is preserve_raw
            else ResultCodec(include_raw_content=preserve_raw)
        )
        payload = codec.encode(result)
        updated = replace(
            record,
            status=result_status(result),
            payload=payload,
            payload_hash=payload_hash(payload),
            updated_at=self._clock(),
        )
        return self.store.update(updated, expected_revision=expected)

    def load_record(
        self,
        job_id: str,
        item_id: str,
        *,
        expected_definition_hash: str | None = None,
    ) -> StoredResultRecord | None:
        record = self.store.load(job_id, item_id)
        if (
            record is not None
            and expected_definition_hash is not None
            and record.definition_hash != expected_definition_hash
        ):
            raise ResultDefinitionMismatchError(
                "Durable result does not match current job definition"
            )
        return record

    def load_result(
        self,
        job_id: str,
        item_id: str,
        *,
        expected_definition_hash: str | None = None,
    ) -> tuple[StoredResultRecord, URLExtractionResult] | None:
        record = self.load_record(
            job_id,
            item_id,
            expected_definition_hash=expected_definition_hash,
        )
        if record is None:
            return None
        return record, self.codec.decode(record.payload)

    def pending_reviews(self, *, job_id: str | None = None) -> tuple[StoredResultRecord, ...]:
        return tuple(self.store.list(job_id=job_id, status=StoredResultStatus.REVIEW_REQUIRED))

    def clear_job(self, job_id: str) -> None:
        self.store.clear_job(job_id)
