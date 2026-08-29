from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.shared.db import session_scope


class RetirementMode(StrEnum):
    OBSERVING = "observing"
    GENERIC_PRIMARY = "generic_primary"


@dataclass(frozen=True, slots=True)
class ParityRetirementPolicy:
    """Conservative live-evidence threshold for reducing the legacy oracle.

    Promotion is automatic only after a clean consecutive evidence window.  The
    first retirement stage never removes the legacy adapter: generic-primary
    pages are sampled against it and generic failures still invoke it as an
    emergency fallback.
    """

    min_consecutive_pass_pages: int = 30
    min_clean_runs: int = 3
    sample_every: int = 10

    def __post_init__(self) -> None:
        if self.min_consecutive_pass_pages < 1:
            raise ValueError("min_consecutive_pass_pages must be >= 1")
        if self.min_clean_runs < 1:
            raise ValueError("min_clean_runs must be >= 1")
        if self.sample_every < 1:
            raise ValueError("sample_every must be >= 1")


@dataclass(frozen=True, slots=True)
class SourceParityState:
    source_key: str
    mode: RetirementMode = RetirementMode.OBSERVING
    parity_observed_pages: int = 0
    parity_pass_pages: int = 0
    parity_failure_pages: int = 0
    consecutive_pass_pages: int = 0
    clean_runs: int = 0
    generic_direct_pages: int = 0
    emergency_fallback_pages: int = 0
    last_failure_at: datetime | None = None
    last_failure_reason: str | None = None
    updated_at: datetime | None = None

    @property
    def parity_rate(self) -> float | None:
        if not self.parity_observed_pages:
            return None
        return self.parity_pass_pages / self.parity_observed_pages


@dataclass(frozen=True, slots=True)
class ParityRunTelemetry:
    source_key: str
    mode_before: RetirementMode
    parity_observed_pages: int = 0
    parity_pass_pages: int = 0
    parity_failure_pages: int = 0
    generic_direct_pages: int = 0
    emergency_fallback_pages: int = 0
    failure_reasons: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return self.parity_observed_pages > 0 and self.parity_failure_pages == 0


@dataclass(frozen=True, slots=True)
class ParityReportRow:
    source_key: str
    mode: str
    parity_observed_pages: int
    parity_pass_pages: int
    parity_failure_pages: int
    parity_rate: float | None
    consecutive_pass_pages: int
    clean_runs: int
    generic_direct_pages: int
    emergency_fallback_pages: int
    last_failure_at: datetime | None
    last_failure_reason: str | None
    updated_at: datetime | None


_DEFAULT_STATE_TABLE = "source_parity_state"
_MAX_FAILURE_REASON = 1000


def _bounded_reason(reasons: Iterable[str]) -> str | None:
    value = " | ".join(str(item) for item in reasons if item)
    if not value:
        return None
    return value if len(value) <= _MAX_FAILURE_REASON else value[: _MAX_FAILURE_REASON - 3] + "..."


def advance_parity_state(
    state: SourceParityState,
    telemetry: ParityRunTelemetry,
    *,
    policy: ParityRetirementPolicy | None = None,
    now: datetime | None = None,
) -> SourceParityState:
    """Apply one source run without allowing unobserved pages to prove parity."""

    policy = policy or ParityRetirementPolicy()
    now = now or datetime.now(UTC)
    failures = max(0, telemetry.parity_failure_pages)
    passes = max(0, telemetry.parity_pass_pages)
    observed = max(0, telemetry.parity_observed_pages)
    if passes + failures > observed:
        observed = passes + failures

    mode = state.mode
    consecutive = state.consecutive_pass_pages
    clean_runs = state.clean_runs
    last_failure_at = state.last_failure_at
    last_failure_reason = state.last_failure_reason

    if failures:
        # Any live discrepancy revokes generic-primary immediately.  Historical
        # aggregate counters are retained, but promotion must build a fresh
        # consecutive window.
        mode = RetirementMode.OBSERVING
        consecutive = 0
        clean_runs = 0
        last_failure_at = now
        last_failure_reason = _bounded_reason(telemetry.failure_reasons) or "parity_failure"
    elif observed:
        consecutive += passes
        clean_runs += 1
        if (
            mode is RetirementMode.OBSERVING
            and consecutive >= policy.min_consecutive_pass_pages
            and clean_runs >= policy.min_clean_runs
        ):
            mode = RetirementMode.GENERIC_PRIMARY

    return replace(
        state,
        mode=mode,
        parity_observed_pages=state.parity_observed_pages + observed,
        parity_pass_pages=state.parity_pass_pages + passes,
        parity_failure_pages=state.parity_failure_pages + failures,
        consecutive_pass_pages=consecutive,
        clean_runs=clean_runs,
        generic_direct_pages=state.generic_direct_pages + max(0, telemetry.generic_direct_pages),
        emergency_fallback_pages=state.emergency_fallback_pages + max(0, telemetry.emergency_fallback_pages),
        last_failure_at=last_failure_at,
        last_failure_reason=last_failure_reason,
        updated_at=now,
    )


def load_parity_state(source_key: str) -> SourceParityState:
    """Read persisted state; pre-0010 databases safely behave as observing."""

    try:
        with session_scope() as session:
            row = session.execute(
                text(
                    f"SELECT source_key, mode, parity_observed_pages, parity_pass_pages, "
                    f"parity_failure_pages, consecutive_pass_pages, clean_runs, "
                    f"generic_direct_pages, emergency_fallback_pages, last_failure_at, "
                    f"last_failure_reason, updated_at FROM {_DEFAULT_STATE_TABLE} "
                    "WHERE source_key = :source_key"
                ),
                {"source_key": source_key},
            ).mappings().first()
    except SQLAlchemyError:
        return SourceParityState(source_key=source_key)

    if row is None:
        return SourceParityState(source_key=source_key)
    try:
        mode = RetirementMode(str(row["mode"]))
    except ValueError:
        mode = RetirementMode.OBSERVING
    return SourceParityState(
        source_key=str(row["source_key"]),
        mode=mode,
        parity_observed_pages=int(row["parity_observed_pages"] or 0),
        parity_pass_pages=int(row["parity_pass_pages"] or 0),
        parity_failure_pages=int(row["parity_failure_pages"] or 0),
        consecutive_pass_pages=int(row["consecutive_pass_pages"] or 0),
        clean_runs=int(row["clean_runs"] or 0),
        generic_direct_pages=int(row["generic_direct_pages"] or 0),
        emergency_fallback_pages=int(row["emergency_fallback_pages"] or 0),
        last_failure_at=row["last_failure_at"],
        last_failure_reason=row["last_failure_reason"],
        updated_at=row["updated_at"],
    )


def persist_parity_run(
    source_key: str,
    telemetry: ParityRunTelemetry,
    *,
    policy: ParityRetirementPolicy | None = None,
) -> SourceParityState:
    """Atomically fold one live run into the bounded per-source aggregate."""

    policy = policy or ParityRetirementPolicy()
    current = load_parity_state(source_key)
    updated = advance_parity_state(current, telemetry, policy=policy)
    try:
        with session_scope() as session:
            session.execute(
                text(
                    f"INSERT INTO {_DEFAULT_STATE_TABLE} ("
                    "source_key, mode, parity_observed_pages, parity_pass_pages, parity_failure_pages, "
                    "consecutive_pass_pages, clean_runs, generic_direct_pages, emergency_fallback_pages, "
                    "last_failure_at, last_failure_reason, updated_at) VALUES ("
                    ":source_key, :mode, :observed, :passes, :failures, :consecutive, :clean_runs, "
                    ":direct, :emergency, :last_failure_at, :last_failure_reason, :updated_at) "
                    "ON CONFLICT(source_key) DO UPDATE SET "
                    "mode=excluded.mode, parity_observed_pages=excluded.parity_observed_pages, "
                    "parity_pass_pages=excluded.parity_pass_pages, "
                    "parity_failure_pages=excluded.parity_failure_pages, "
                    "consecutive_pass_pages=excluded.consecutive_pass_pages, clean_runs=excluded.clean_runs, "
                    "generic_direct_pages=excluded.generic_direct_pages, "
                    "emergency_fallback_pages=excluded.emergency_fallback_pages, "
                    "last_failure_at=excluded.last_failure_at, "
                    "last_failure_reason=excluded.last_failure_reason, updated_at=excluded.updated_at"
                ),
                {
                    "source_key": updated.source_key,
                    "mode": updated.mode.value,
                    "observed": updated.parity_observed_pages,
                    "passes": updated.parity_pass_pages,
                    "failures": updated.parity_failure_pages,
                    "consecutive": updated.consecutive_pass_pages,
                    "clean_runs": updated.clean_runs,
                    "direct": updated.generic_direct_pages,
                    "emergency": updated.emergency_fallback_pages,
                    "last_failure_at": updated.last_failure_at,
                    "last_failure_reason": updated.last_failure_reason,
                    "updated_at": updated.updated_at,
                },
            )
    except SQLAlchemyError:
        # Collection must not fail merely because an older/pre-migration local
        # database cannot persist telemetry.  Alembic 0010 restores durability.
        return updated
    return updated


def parity_report() -> tuple[ParityReportRow, ...]:
    try:
        with session_scope() as session:
            rows = session.execute(
                text(
                    f"SELECT source_key, mode, parity_observed_pages, parity_pass_pages, "
                    f"parity_failure_pages, consecutive_pass_pages, clean_runs, generic_direct_pages, "
                    f"emergency_fallback_pages, last_failure_at, last_failure_reason, updated_at "
                    f"FROM {_DEFAULT_STATE_TABLE} ORDER BY source_key"
                )
            ).mappings().all()
    except SQLAlchemyError:
        return ()

    result: list[ParityReportRow] = []
    for row in rows:
        observed = int(row["parity_observed_pages"] or 0)
        passes = int(row["parity_pass_pages"] or 0)
        result.append(
            ParityReportRow(
                source_key=str(row["source_key"]),
                mode=str(row["mode"]),
                parity_observed_pages=observed,
                parity_pass_pages=passes,
                parity_failure_pages=int(row["parity_failure_pages"] or 0),
                parity_rate=(passes / observed) if observed else None,
                consecutive_pass_pages=int(row["consecutive_pass_pages"] or 0),
                clean_runs=int(row["clean_runs"] or 0),
                generic_direct_pages=int(row["generic_direct_pages"] or 0),
                emergency_fallback_pages=int(row["emergency_fallback_pages"] or 0),
                last_failure_at=row["last_failure_at"],
                last_failure_reason=row["last_failure_reason"],
                updated_at=row["updated_at"],
            )
        )
    return tuple(result)
