from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Protocol, Sequence

from .models import (
    ReviewAction,
    ReviewAuditEvent,
    ReviewLease,
    ReviewLeaseConflictError,
    ReviewLeaseExpiredError,
    ReviewLeaseNotFoundError,
    ReviewerMismatchError,
)


class ReviewQueueStore(Protocol):
    def load_lease(self, job_id: str, item_id: str) -> ReviewLease | None: ...
    def claim(self, job_id: str, item_id: str, reviewer_id: str, lease_token: str, *, now: float, ttl_s: float, event_id: str) -> tuple[ReviewLease, ReviewAuditEvent | None]: ...
    def renew(self, job_id: str, item_id: str, reviewer_id: str, lease_token: str, *, now: float, ttl_s: float, event_id: str) -> tuple[ReviewLease, ReviewAuditEvent]: ...
    def release(self, job_id: str, item_id: str, reviewer_id: str, lease_token: str, *, now: float, event_id: str, completed: bool = False) -> ReviewAuditEvent: ...
    def append_event(self, event: ReviewAuditEvent) -> None: ...
    def list_events(self, *, job_id: str | None = None, item_id: str | None = None) -> Sequence[ReviewAuditEvent]: ...


def _new_lease(job_id: str, item_id: str, reviewer_id: str, lease_token: str, *, now: float, ttl_s: float, revision: int, claimed_at: float | None = None) -> ReviewLease:
    if ttl_s <= 0:
        raise ValueError("ttl_s must be positive")
    return ReviewLease(
        job_id=job_id,
        item_id=item_id,
        reviewer_id=reviewer_id,
        lease_token=lease_token,
        claimed_at=now if claimed_at is None else claimed_at,
        expires_at=now + ttl_s,
        updated_at=now,
        revision=revision,
    )


def _check_owner(lease: ReviewLease | None, reviewer_id: str, lease_token: str, *, now: float, require_active: bool = True) -> ReviewLease:
    if lease is None:
        raise ReviewLeaseNotFoundError("Review lease does not exist")
    if lease.reviewer_id != reviewer_id:
        raise ReviewerMismatchError("Review lease belongs to another reviewer")
    if lease.lease_token != lease_token:
        raise ReviewLeaseConflictError("Review lease token does not match")
    if require_active and not lease.active(now):
        raise ReviewLeaseExpiredError("Review lease has expired")
    return lease


def _lease_event(event_id: str, lease: ReviewLease, action: ReviewAction, *, at: float, metadata: dict | None = None) -> ReviewAuditEvent:
    return ReviewAuditEvent(
        event_id=event_id,
        job_id=lease.job_id,
        item_id=lease.item_id,
        action=action,
        reviewer_id=lease.reviewer_id,
        at=at,
        lease_revision=lease.revision,
        metadata={} if metadata is None else metadata,
    )


class InMemoryReviewQueueStore:
    def __init__(self) -> None:
        self._leases: dict[tuple[str, str], ReviewLease] = {}
        self._events: list[ReviewAuditEvent] = []
        self._event_ids: set[str] = set()

    def load_lease(self, job_id: str, item_id: str) -> ReviewLease | None:
        lease = self._leases.get((job_id, item_id))
        return None if lease is None else ReviewLease.from_dict(lease.to_dict())

    def claim(self, job_id, item_id, reviewer_id, lease_token, *, now, ttl_s, event_id):
        key = (job_id, item_id)
        current = self._leases.get(key)
        if current is not None and current.active(now):
            if current.reviewer_id == reviewer_id:
                return ReviewLease.from_dict(current.to_dict()), None
            raise ReviewLeaseConflictError("Review item is already claimed")
        revision = 1 if current is None else current.revision + 1
        lease = _new_lease(job_id, item_id, reviewer_id, lease_token, now=now, ttl_s=ttl_s, revision=revision)
        action = ReviewAction.CLAIMED if current is None else ReviewAction.TAKEN_OVER
        metadata = {} if current is None else {"previous_reviewer_id": current.reviewer_id}
        event = _lease_event(event_id, lease, action, at=now, metadata=metadata)
        self._leases[key] = lease
        self.append_event(event)
        return ReviewLease.from_dict(lease.to_dict()), event

    def renew(self, job_id, item_id, reviewer_id, lease_token, *, now, ttl_s, event_id):
        key = (job_id, item_id)
        current = _check_owner(self._leases.get(key), reviewer_id, lease_token, now=now)
        lease = _new_lease(job_id, item_id, reviewer_id, lease_token, now=now, ttl_s=ttl_s, revision=current.revision + 1, claimed_at=current.claimed_at)
        event = _lease_event(event_id, lease, ReviewAction.RENEWED, at=now)
        self._leases[key] = lease
        self.append_event(event)
        return ReviewLease.from_dict(lease.to_dict()), event

    def release(self, job_id, item_id, reviewer_id, lease_token, *, now, event_id, completed=False):
        key = (job_id, item_id)
        current = _check_owner(self._leases.get(key), reviewer_id, lease_token, now=now, require_active=not completed)
        action = ReviewAction.COMPLETED if completed else ReviewAction.RELEASED
        event = _lease_event(event_id, current, action, at=now)
        self._leases.pop(key, None)
        self.append_event(event)
        return event

    def append_event(self, event: ReviewAuditEvent) -> None:
        if event.event_id in self._event_ids:
            raise ReviewLeaseConflictError("Review audit event_id already exists")
        self._event_ids.add(event.event_id)
        self._events.append(ReviewAuditEvent.from_dict(event.to_dict()))

    def list_events(self, *, job_id=None, item_id=None):
        events = self._events
        if job_id is not None:
            events = [event for event in events if event.job_id == job_id]
        if item_id is not None:
            events = [event for event in events if event.item_id == item_id]
        return tuple(ReviewAuditEvent.from_dict(event.to_dict()) for event in sorted(events, key=lambda e: (e.at, e.event_id)))


class JsonReviewQueueStore(InMemoryReviewQueueStore):
    """Atomic local queue/audit store for a single writer process."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        super().__init__()
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if int(payload.get("version", -1)) != 1:
                raise ValueError("Unsupported review queue store version")
            for raw in payload.get("leases", []):
                lease = ReviewLease.from_dict(raw)
                self._leases[(lease.job_id, lease.item_id)] = lease
            for raw in payload.get("events", []):
                event = ReviewAuditEvent.from_dict(raw)
                self._events.append(event)
                self._event_ids.add(event.event_id)

    def claim(self, *args, **kwargs):
        result = super().claim(*args, **kwargs)
        self._persist()
        return result

    def renew(self, *args, **kwargs):
        result = super().renew(*args, **kwargs)
        self._persist()
        return result

    def release(self, *args, **kwargs):
        result = super().release(*args, **kwargs)
        self._persist()
        return result

    def append_event(self, event: ReviewAuditEvent) -> None:
        super().append_event(event)
        self._persist()

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "version": 1,
                "leases": [lease.to_dict() for _, lease in sorted(self._leases.items())],
                "events": [event.to_dict() for event in self._events],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


class SQLiteReviewQueueStore:
    """WAL-backed lease/audit store for multiple review workers on one runtime node."""

    def __init__(self, path: str | os.PathLike[str], *, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000.0, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("CREATE TABLE IF NOT EXISTS review_leases (job_id TEXT NOT NULL,item_id TEXT NOT NULL,lease_json TEXT NOT NULL,expires_at REAL NOT NULL,PRIMARY KEY(job_id,item_id))")
            self._connection.execute("CREATE TABLE IF NOT EXISTS review_audit (event_id TEXT PRIMARY KEY,job_id TEXT NOT NULL,item_id TEXT NOT NULL,at REAL NOT NULL,event_json TEXT NOT NULL)")
            self._connection.execute("CREATE INDEX IF NOT EXISTS idx_review_audit_item ON review_audit(job_id,item_id,at)")

    @staticmethod
    def _serialize_lease(lease: ReviewLease) -> str:
        return json.dumps(lease.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _serialize_event(event: ReviewAuditEvent) -> str:
        return json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _load_lease_locked(self, job_id: str, item_id: str) -> ReviewLease | None:
        row = self._connection.execute("SELECT lease_json FROM review_leases WHERE job_id=? AND item_id=?", (job_id, item_id)).fetchone()
        return None if row is None else ReviewLease.from_dict(json.loads(row["lease_json"]))

    def load_lease(self, job_id: str, item_id: str) -> ReviewLease | None:
        with self._lock:
            return self._load_lease_locked(job_id, item_id)

    def _insert_event_locked(self, event: ReviewAuditEvent) -> None:
        try:
            self._connection.execute("INSERT INTO review_audit(event_id,job_id,item_id,at,event_json) VALUES(?,?,?,?,?)", (event.event_id, event.job_id, event.item_id, event.at, self._serialize_event(event)))
        except sqlite3.IntegrityError as exc:
            raise ReviewLeaseConflictError("Review audit event_id already exists") from exc

    def claim(self, job_id, item_id, reviewer_id, lease_token, *, now, ttl_s, event_id):
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._load_lease_locked(job_id, item_id)
                if current is not None and current.active(now):
                    if current.reviewer_id == reviewer_id:
                        self._connection.execute("COMMIT")
                        return current, None
                    raise ReviewLeaseConflictError("Review item is already claimed")
                revision = 1 if current is None else current.revision + 1
                lease = _new_lease(job_id, item_id, reviewer_id, lease_token, now=now, ttl_s=ttl_s, revision=revision)
                action = ReviewAction.CLAIMED if current is None else ReviewAction.TAKEN_OVER
                metadata = {} if current is None else {"previous_reviewer_id": current.reviewer_id}
                event = _lease_event(event_id, lease, action, at=now, metadata=metadata)
                self._connection.execute("INSERT INTO review_leases(job_id,item_id,lease_json,expires_at) VALUES(?,?,?,?) ON CONFLICT(job_id,item_id) DO UPDATE SET lease_json=excluded.lease_json,expires_at=excluded.expires_at", (job_id, item_id, self._serialize_lease(lease), lease.expires_at))
                self._insert_event_locked(event)
                self._connection.execute("COMMIT")
                return lease, event
            except Exception:
                self._rollback()
                raise

    def renew(self, job_id, item_id, reviewer_id, lease_token, *, now, ttl_s, event_id):
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = _check_owner(self._load_lease_locked(job_id, item_id), reviewer_id, lease_token, now=now)
                lease = _new_lease(job_id, item_id, reviewer_id, lease_token, now=now, ttl_s=ttl_s, revision=current.revision + 1, claimed_at=current.claimed_at)
                event = _lease_event(event_id, lease, ReviewAction.RENEWED, at=now)
                self._connection.execute("UPDATE review_leases SET lease_json=?,expires_at=? WHERE job_id=? AND item_id=?", (self._serialize_lease(lease), lease.expires_at, job_id, item_id))
                self._insert_event_locked(event)
                self._connection.execute("COMMIT")
                return lease, event
            except Exception:
                self._rollback()
                raise

    def release(self, job_id, item_id, reviewer_id, lease_token, *, now, event_id, completed=False):
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = _check_owner(self._load_lease_locked(job_id, item_id), reviewer_id, lease_token, now=now, require_active=not completed)
                action = ReviewAction.COMPLETED if completed else ReviewAction.RELEASED
                event = _lease_event(event_id, current, action, at=now)
                self._connection.execute("DELETE FROM review_leases WHERE job_id=? AND item_id=?", (job_id, item_id))
                self._insert_event_locked(event)
                self._connection.execute("COMMIT")
                return event
            except Exception:
                self._rollback()
                raise

    def append_event(self, event: ReviewAuditEvent) -> None:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._insert_event_locked(event)
                self._connection.execute("COMMIT")
            except Exception:
                self._rollback()
                raise

    def list_events(self, *, job_id=None, item_id=None):
        where: list[str] = []
        params: list[str] = []
        if job_id is not None:
            where.append("job_id=?")
            params.append(job_id)
        if item_id is not None:
            where.append("item_id=?")
            params.append(item_id)
        sql = "SELECT event_json FROM review_audit"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY at,event_id"
        with self._lock:
            rows = self._connection.execute(sql, tuple(params)).fetchall()
        return tuple(ReviewAuditEvent.from_dict(json.loads(row["event_json"])) for row in rows)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteReviewQueueStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
