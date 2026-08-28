from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .profiles import EvidenceFingerprint, ProfileSignalStats


PROFILE_SCHEMA_VERSION = 2
_SECONDS_PER_DAY = 86_400.0


@dataclass(frozen=True, slots=True)
class ProfileLifecyclePolicy:
    decay_half_life_days: float = 30.0
    max_signal_age_days: float = 180.0
    prune_effective_weight_below: float = 0.01

    def __post_init__(self) -> None:
        if self.decay_half_life_days <= 0:
            raise ValueError("decay_half_life_days must be positive")
        if self.max_signal_age_days <= 0:
            raise ValueError("max_signal_age_days must be positive")
        if self.prune_effective_weight_below < 0:
            raise ValueError("prune_effective_weight_below must be non-negative")

    def effective(
        self,
        confirmations: float,
        rejections: float,
        updated_at: float,
        *,
        as_of: float,
    ) -> ProfileSignalStats:
        age_seconds = float(int(max(0.0, as_of - updated_at)))
        if age_seconds >= self.max_signal_age_days * _SECONDS_PER_DAY:
            return ProfileSignalStats()
        factor = 0.5 ** (
            age_seconds / (self.decay_half_life_days * _SECONDS_PER_DAY)
        )
        return ProfileSignalStats(
            confirmations=confirmations * factor,
            rejections=rejections * factor,
        )

    def should_prune(
        self,
        confirmations: float,
        rejections: float,
        updated_at: float,
        *,
        as_of: float,
    ) -> bool:
        age_seconds = float(int(max(0.0, as_of - updated_at)))
        if age_seconds >= self.max_signal_age_days * _SECONDS_PER_DAY:
            return True
        effective = self.effective(
            confirmations,
            rejections,
            updated_at,
            as_of=as_of,
        )
        return (
            float(effective.confirmations) + float(effective.rejections)
            <= self.prune_effective_weight_below
        )


@dataclass(frozen=True, slots=True)
class ProfilePruneReport:
    removed_patterns: int
    removed_fields: int
    removed_sites: int
    revision: int
    as_of: float

    @property
    def changed(self) -> bool:
        return self.removed_patterns > 0


class SiteProfileStore(Protocol):
    schema_version: int

    @property
    def revision(self) -> int: ...

    def get_stats(
        self,
        site_key: str,
        field_key: str,
        fingerprint: EvidenceFingerprint,
    ) -> ProfileSignalStats: ...

    def record(
        self,
        site_key: str,
        field_key: str,
        *,
        positive: Sequence[EvidenceFingerprint] = (),
        negative: Sequence[EvidenceFingerprint] = (),
    ) -> None: ...

    def prune(self) -> ProfilePruneReport: ...

    def snapshot(self) -> Mapping[str, Any]: ...


class _LifecycleBase:
    schema_version = PROFILE_SCHEMA_VERSION

    def __init__(
        self,
        *,
        lifecycle: ProfileLifecyclePolicy | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.lifecycle = lifecycle or ProfileLifecyclePolicy()
        self._clock = clock or time.time

    @staticmethod
    def _raw(
        confirmations: float,
        rejections: float,
        updated_at: float,
    ) -> dict[str, float]:
        if confirmations < 0 or rejections < 0:
            raise ValueError("Profile counters must be non-negative")
        if not all(
            math.isfinite(value)
            for value in (confirmations, rejections, updated_at)
        ):
            raise ValueError("Profile counters/timestamps must be finite")
        if updated_at < 0:
            raise ValueError("Profile updated_at must be non-negative")
        return {
            "confirmations": float(confirmations),
            "rejections": float(rejections),
            "updated_at": float(updated_at),
        }

    def _effective_raw(
        self,
        raw: Mapping[str, Any],
        *,
        as_of: float,
    ) -> ProfileSignalStats:
        return self.lifecycle.effective(
            float(raw.get("confirmations", 0.0)),
            float(raw.get("rejections", 0.0)),
            float(raw["updated_at"]),
            as_of=as_of,
        )

    def _materialize(
        self,
        raw: Mapping[str, Any] | None,
        *,
        as_of: float,
    ) -> dict[str, float]:
        if raw is None:
            return self._raw(0.0, 0.0, as_of)
        effective = self._effective_raw(raw, as_of=as_of)
        return self._raw(
            float(effective.confirmations),
            float(effective.rejections),
            as_of,
        )


class InMemorySiteProfileStore(_LifecycleBase):
    """Lifecycle-aware value-free store used by the default pipeline."""

    def __init__(
        self,
        data: Mapping[str, Any] | None = None,
        *,
        lifecycle: ProfileLifecyclePolicy | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(lifecycle=lifecycle, clock=clock)
        self._sites: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
        self._revision = 0
        if data:
            self._load(data)

    @property
    def revision(self) -> int:
        return self._revision

    def _load(self, data: Mapping[str, Any]) -> None:
        version = int(data.get("version", 1)) if "sites" in data else 1
        if version not in {1, PROFILE_SCHEMA_VERSION}:
            raise ValueError("Unsupported site profile store version")
        self._revision = int(data.get("revision", 0))
        if self._revision < 0:
            raise ValueError("Profile revision must be non-negative")
        sites = data.get("sites", data)
        if not isinstance(sites, Mapping):
            raise ValueError("Profile store data must contain a mapping of sites")
        migrated_at = self._clock()
        for site_key, fields in sites.items():
            if not isinstance(fields, Mapping):
                continue
            for field_key, patterns in fields.items():
                if not isinstance(patterns, Mapping):
                    continue
                for fingerprint_key, raw in patterns.items():
                    if not isinstance(raw, Mapping):
                        continue
                    updated_at = float(raw.get("updated_at", migrated_at))
                    stored = self._raw(
                        float(raw.get("confirmations", 0.0)),
                        float(raw.get("rejections", 0.0)),
                        updated_at,
                    )
                    self._sites.setdefault(str(site_key), {}).setdefault(
                        str(field_key), {}
                    )[str(fingerprint_key)] = stored

    def get_stats(
        self,
        site_key: str,
        field_key: str,
        fingerprint: EvidenceFingerprint,
    ) -> ProfileSignalStats:
        raw = (
            self._sites.get(site_key, {})
            .get(field_key, {})
            .get(fingerprint.key)
        )
        if raw is None:
            return ProfileSignalStats()
        return self._effective_raw(raw, as_of=self._clock())

    def record(
        self,
        site_key: str,
        field_key: str,
        *,
        positive: Sequence[EvidenceFingerprint] = (),
        negative: Sequence[EvidenceFingerprint] = (),
    ) -> None:
        positive_keys = {item.key for item in positive}
        negative_keys = {
            item.key for item in negative if item.key not in positive_keys
        }
        if not positive_keys and not negative_keys:
            return
        as_of = self._clock()
        field = self._sites.setdefault(site_key, {}).setdefault(field_key, {})
        for key in positive_keys:
            raw = self._materialize(field.get(key), as_of=as_of)
            raw["confirmations"] += 1.0
            field[key] = raw
        for key in negative_keys:
            raw = self._materialize(field.get(key), as_of=as_of)
            raw["rejections"] += 1.0
            field[key] = raw
        self._revision += 1

    def prune(self) -> ProfilePruneReport:
        as_of = self._clock()
        before_fields = sum(len(fields) for fields in self._sites.values())
        before_sites = len(self._sites)
        removed = 0
        for site_key in list(self._sites):
            fields = self._sites[site_key]
            for field_key in list(fields):
                patterns = fields[field_key]
                for fingerprint_key in list(patterns):
                    raw = patterns[fingerprint_key]
                    if self.lifecycle.should_prune(
                        float(raw["confirmations"]),
                        float(raw["rejections"]),
                        float(raw["updated_at"]),
                        as_of=as_of,
                    ):
                        del patterns[fingerprint_key]
                        removed += 1
                if not patterns:
                    del fields[field_key]
            if not fields:
                del self._sites[site_key]
        after_fields = sum(len(fields) for fields in self._sites.values())
        after_sites = len(self._sites)
        if removed:
            self._revision += 1
        return ProfilePruneReport(
            removed_patterns=removed,
            removed_fields=before_fields - after_fields,
            removed_sites=before_sites - after_sites,
            revision=self._revision,
            as_of=as_of,
        )

    def snapshot(self) -> Mapping[str, Any]:
        return json.loads(
            json.dumps(
                {
                    "version": PROFILE_SCHEMA_VERSION,
                    "revision": self._revision,
                    "sites": self._sites,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )


class JsonSiteProfileStore(InMemorySiteProfileStore):
    """Atomic local JSON backend with automatic v1 -> v2 migration."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        lifecycle: ProfileLifecyclePolicy | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.path = Path(path)
        data: Mapping[str, Any] | None = None
        migrated = False
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            version = int(payload.get("version", 1))
            if version not in {1, PROFILE_SCHEMA_VERSION}:
                raise ValueError("Unsupported site profile store version")
            data = payload
            migrated = version == 1
        super().__init__(data=data, lifecycle=lifecycle, clock=clock)
        if migrated:
            self._persist()

    def record(
        self,
        site_key: str,
        field_key: str,
        *,
        positive: Sequence[EvidenceFingerprint] = (),
        negative: Sequence[EvidenceFingerprint] = (),
    ) -> None:
        before = self.revision
        super().record(
            site_key,
            field_key,
            positive=positive,
            negative=negative,
        )
        if self.revision != before:
            self._persist()

    def prune(self) -> ProfilePruneReport:
        report = super().prune()
        if report.changed:
            self._persist()
        return report

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            self.snapshot(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
            text=True,
        )
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


class SQLiteSiteProfileStore(_LifecycleBase):
    """WAL-backed transactional store for multiple processes on one node."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        lifecycle: ProfileLifecyclePolicy | None = None,
        clock: Callable[[], float] | None = None,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        super().__init__(lifecycle=lifecycle, clock=clock)
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
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_signals (
                site_key TEXT NOT NULL,
                field_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                confirmations REAL NOT NULL,
                rejections REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (site_key, field_key, fingerprint)
            )
            """
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO profile_meta(key,value) VALUES('schema_version',?)",
            (str(PROFILE_SCHEMA_VERSION),),
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO profile_meta(key,value) VALUES('revision','0')"
        )
        row = self._connection.execute(
            "SELECT value FROM profile_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None or int(row["value"]) != PROFILE_SCHEMA_VERSION:
            raise ValueError("Unsupported SQLite site profile schema version")

    @property
    def revision(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM profile_meta WHERE key='revision'"
            ).fetchone()
            return int(row["value"]) if row is not None else 0

    def _set_revision(self, revision: int) -> None:
        self._connection.execute(
            """
            INSERT INTO profile_meta(key,value) VALUES('revision',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(revision),),
        )

    def get_stats(
        self,
        site_key: str,
        field_key: str,
        fingerprint: EvidenceFingerprint,
    ) -> ProfileSignalStats:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT confirmations,rejections,updated_at
                FROM profile_signals
                WHERE site_key=? AND field_key=? AND fingerprint=?
                """,
                (site_key, field_key, fingerprint.key),
            ).fetchone()
        if row is None:
            return ProfileSignalStats()
        return self.lifecycle.effective(
            float(row["confirmations"]),
            float(row["rejections"]),
            float(row["updated_at"]),
            as_of=self._clock(),
        )

    def record(
        self,
        site_key: str,
        field_key: str,
        *,
        positive: Sequence[EvidenceFingerprint] = (),
        negative: Sequence[EvidenceFingerprint] = (),
    ) -> None:
        positive_keys = {item.key for item in positive}
        negative_keys = {
            item.key for item in negative if item.key not in positive_keys
        }
        if not positive_keys and not negative_keys:
            return
        as_of = self._clock()
        changes = (
            [(key, 1.0, 0.0) for key in positive_keys]
            + [(key, 0.0, 1.0) for key in negative_keys]
        )
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                for key, positive_increment, negative_increment in changes:
                    row = self._connection.execute(
                        """
                        SELECT confirmations,rejections,updated_at
                        FROM profile_signals
                        WHERE site_key=? AND field_key=? AND fingerprint=?
                        """,
                        (site_key, field_key, key),
                    ).fetchone()
                    raw = (
                        {
                            "confirmations": row["confirmations"],
                            "rejections": row["rejections"],
                            "updated_at": row["updated_at"],
                        }
                        if row is not None
                        else None
                    )
                    materialized = self._materialize(raw, as_of=as_of)
                    materialized["confirmations"] += positive_increment
                    materialized["rejections"] += negative_increment
                    self._connection.execute(
                        """
                        INSERT INTO profile_signals(
                            site_key,field_key,fingerprint,
                            confirmations,rejections,updated_at
                        ) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(site_key,field_key,fingerprint) DO UPDATE SET
                            confirmations=excluded.confirmations,
                            rejections=excluded.rejections,
                            updated_at=excluded.updated_at
                        """,
                        (
                            site_key,
                            field_key,
                            key,
                            materialized["confirmations"],
                            materialized["rejections"],
                            materialized["updated_at"],
                        ),
                    )
                self._set_revision(self.revision + 1)
                self._connection.execute("COMMIT")
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def prune(self) -> ProfilePruneReport:
        as_of = self._clock()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                rows = self._connection.execute(
                    """
                    SELECT site_key,field_key,fingerprint,
                           confirmations,rejections,updated_at
                    FROM profile_signals
                    """
                ).fetchall()
                before_fields = {
                    (row["site_key"], row["field_key"]) for row in rows
                }
                before_sites = {row["site_key"] for row in rows}
                removed = 0
                for row in rows:
                    if self.lifecycle.should_prune(
                        float(row["confirmations"]),
                        float(row["rejections"]),
                        float(row["updated_at"]),
                        as_of=as_of,
                    ):
                        self._connection.execute(
                            """
                            DELETE FROM profile_signals
                            WHERE site_key=? AND field_key=? AND fingerprint=?
                            """,
                            (
                                row["site_key"],
                                row["field_key"],
                                row["fingerprint"],
                            ),
                        )
                        removed += 1
                remaining = self._connection.execute(
                    "SELECT DISTINCT site_key,field_key FROM profile_signals"
                ).fetchall()
                after_fields = {
                    (row["site_key"], row["field_key"]) for row in remaining
                }
                after_sites = {row["site_key"] for row in remaining}
                revision = self.revision
                if removed:
                    revision += 1
                    self._set_revision(revision)
                self._connection.execute("COMMIT")
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
        return ProfilePruneReport(
            removed_patterns=removed,
            removed_fields=len(before_fields - after_fields),
            removed_sites=len(before_sites - after_sites),
            revision=revision,
            as_of=as_of,
        )

    def snapshot(self) -> Mapping[str, Any]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT site_key,field_key,fingerprint,
                       confirmations,rejections,updated_at
                FROM profile_signals
                ORDER BY site_key,field_key,fingerprint
                """
            ).fetchall()
            revision = self.revision
        sites: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
        for row in rows:
            sites.setdefault(row["site_key"], {}).setdefault(
                row["field_key"], {}
            )[row["fingerprint"]] = {
                "confirmations": float(row["confirmations"]),
                "rejections": float(row["rejections"]),
                "updated_at": float(row["updated_at"]),
            }
        return {
            "version": PROFILE_SCHEMA_VERSION,
            "revision": revision,
            "sites": sites,
        }

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SQLiteSiteProfileStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
