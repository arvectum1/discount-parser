from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable

from src.sources.config import SourceConfig, load_source_configs
from src.sources.parity_telemetry import (
    ParityRetirementPolicy,
    RetirementMode,
    SourceParityState,
    load_parity_state,
)
from src.sources.runner import RunResult, run_all


class EngineAcceptanceStatus(StrEnum):
    PASS = "PASS"
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class ParityStateEvidence:
    mode: str
    parity_observed_pages: int
    parity_pass_pages: int
    parity_failure_pages: int
    parity_rate: float | None
    consecutive_pass_pages: int
    clean_runs: int
    generic_direct_pages: int
    emergency_fallback_pages: int


@dataclass(frozen=True, slots=True)
class SourceEngineAcceptance:
    source_key: str
    status: str
    customer_path_safe: bool
    retirement_evidence_complete: bool
    engine_exercised: bool
    reasons: tuple[str, ...]
    run_count: int
    fetched_total: int
    errors_total: int
    engine_selected_urls: int
    engine_decoded_pages: int
    engine_generic_pages: int
    engine_legacy_pages: int
    engine_runtime_fallback_runs: int
    parity_observed_pages: int
    parity_pass_pages: int
    parity_failure_pages: int
    direct_generic_pages: int
    emergency_fallback_pages: int
    retirement_mode_before: str
    retirement_mode_after: str
    parity_state_before: ParityStateEvidence
    parity_state_after: ParityStateEvidence


@dataclass(frozen=True, slots=True)
class EngineAcceptanceReport:
    schema_version: int
    task: str
    scenario: str
    status: str
    customer_path_safe: bool
    retirement_evidence_complete: bool
    requested_runs: int
    completed_runs: int
    configured_source_keys: tuple[str, ...]
    missing_source_keys: tuple[str, ...]
    source_count: int
    reasons: tuple[str, ...]
    policy: dict[str, int]
    sources: tuple[SourceEngineAcceptance, ...]
    started_at: datetime
    finished_at: datetime
    duration_seconds: float


Runner = Callable[..., list[RunResult]]
ConfigLoader = Callable[[str], list[SourceConfig]]
StateLoader = Callable[[str], SourceParityState]


def _state_evidence(state: SourceParityState) -> ParityStateEvidence:
    return ParityStateEvidence(
        mode=state.mode.value,
        parity_observed_pages=max(0, int(state.parity_observed_pages)),
        parity_pass_pages=max(0, int(state.parity_pass_pages)),
        parity_failure_pages=max(0, int(state.parity_failure_pages)),
        parity_rate=state.parity_rate,
        consecutive_pass_pages=max(0, int(state.consecutive_pass_pages)),
        clean_runs=max(0, int(state.clean_runs)),
        generic_direct_pages=max(0, int(state.generic_direct_pages)),
        emergency_fallback_pages=max(0, int(state.emergency_fallback_pages)),
    )


def _retirement_complete(state: SourceParityState, policy: ParityRetirementPolicy) -> bool:
    return bool(
        state.mode is RetirementMode.GENERIC_PRIMARY
        and state.consecutive_pass_pages >= policy.min_consecutive_pass_pages
        and state.clean_runs >= policy.min_clean_runs
    )


def _sum(results: list[RunResult], attribute: str) -> int:
    return sum(max(0, int(getattr(result, attribute, 0) or 0)) for result in results)


def _evaluate_source(
    config: SourceConfig,
    results: list[RunResult],
    *,
    requested_runs: int,
    state_before: SourceParityState,
    state_after: SourceParityState,
    policy: ParityRetirementPolicy,
) -> SourceEngineAcceptance:
    reasons: list[str] = []

    if config.runtime_mode != "hybrid":
        reasons.append("source_not_hybrid")
    if len(results) != requested_runs:
        reasons.append("missing_run_result")

    fetched_total = _sum(results, "fetched")
    errors_total = _sum(results, "errors")
    empty_fetch_runs = sum(1 for result in results if int(result.fetched or 0) <= 0)
    runtime_fallback_runs = sum(1 for result in results if bool(result.engine_fallback_used))
    selected_urls = _sum(results, "engine_selected_urls")
    decoded_pages = _sum(results, "engine_decoded_pages")
    generic_pages = _sum(results, "engine_generic_pages")
    legacy_pages = _sum(results, "engine_legacy_pages")
    parity_failures = _sum(results, "engine_parity_failures")
    parity_observed = _sum(results, "engine_parity_observed_pages")
    parity_passes = _sum(results, "engine_parity_pass_pages")
    direct_generic = _sum(results, "engine_direct_generic_pages")
    emergency_fallback = _sum(results, "engine_emergency_fallback_pages")

    if errors_total:
        reasons.append("collection_error")
    if empty_fetch_runs:
        reasons.append("empty_fetch")
    if runtime_fallback_runs:
        reasons.append("engine_runtime_fallback")
    if parity_failures:
        reasons.append("engine_parity_failure")
    if emergency_fallback:
        reasons.append("engine_emergency_fallback")

    customer_path_safe = bool(
        len(results) == requested_runs
        and results
        and errors_total == 0
        and empty_fetch_runs == 0
    )

    engine_exercised = bool(selected_urls > 0 and decoded_pages > 0 and generic_pages > 0)
    retirement_complete = _retirement_complete(state_after, policy)

    hard_failure_reasons = {
        "source_not_hybrid",
        "missing_run_result",
        "collection_error",
        "empty_fetch",
        "engine_runtime_fallback",
        "engine_parity_failure",
        "engine_emergency_fallback",
    }
    if any(reason in hard_failure_reasons for reason in reasons):
        status = EngineAcceptanceStatus.FAIL.value
    else:
        if not engine_exercised:
            reasons.append("generic_engine_not_exercised")
        if state_after.mode is RetirementMode.OBSERVING and parity_observed <= 0:
            reasons.append("live_parity_not_observed")
        if not retirement_complete:
            reasons.append("retirement_evidence_incomplete")
        status = (
            EngineAcceptanceStatus.PASS.value
            if engine_exercised and retirement_complete
            else EngineAcceptanceStatus.NEEDS_EVIDENCE.value
        )

    return SourceEngineAcceptance(
        source_key=config.key,
        status=status,
        customer_path_safe=customer_path_safe,
        retirement_evidence_complete=retirement_complete,
        engine_exercised=engine_exercised,
        reasons=tuple(dict.fromkeys(reasons)),
        run_count=len(results),
        fetched_total=fetched_total,
        errors_total=errors_total,
        engine_selected_urls=selected_urls,
        engine_decoded_pages=decoded_pages,
        engine_generic_pages=generic_pages,
        engine_legacy_pages=legacy_pages,
        engine_runtime_fallback_runs=runtime_fallback_runs,
        parity_observed_pages=parity_observed,
        parity_pass_pages=parity_passes,
        parity_failure_pages=parity_failures,
        direct_generic_pages=direct_generic,
        emergency_fallback_pages=emergency_fallback,
        retirement_mode_before=state_before.mode.value,
        retirement_mode_after=state_after.mode.value,
        parity_state_before=_state_evidence(state_before),
        parity_state_after=_state_evidence(state_after),
    )


def run_engine_acceptance(
    path: str = "config/sources.yaml",
    only: str | None = None,
    *,
    runs: int = 1,
    runner: Runner = run_all,
    config_loader: ConfigLoader = load_source_configs,
    state_loader: StateLoader = load_parity_state,
    retirement_policy: ParityRetirementPolicy | None = None,
) -> EngineAcceptanceReport:
    """Exercise the existing production source path and summarize safe DP Engine evidence.

    This function deliberately does not create another fetch/probe stack. Each
    requested cycle is one ordinary production collection through ``run_all``.
    The returned evidence contains counters and stable reason codes only: raw
    HTML, offer values, source URLs, runtime warnings and exception strings are
    intentionally excluded.
    """

    if runs < 1 or runs > 3:
        raise ValueError("runs must be between 1 and 3")

    policy = retirement_policy or ParityRetirementPolicy()
    started_at = datetime.now(UTC)
    configured = [config for config in config_loader(path) if config.enabled]
    if only is not None:
        configured = [config for config in configured if config.key == only]

    configured_keys = tuple(config.key for config in configured)
    global_reasons: list[str] = []
    if only is not None and not configured:
        global_reasons.append("requested_source_not_configured")
    elif not configured:
        global_reasons.append("no_enabled_sources")

    state_before = {config.key: state_loader(config.key) for config in configured}
    results_by_source: dict[str, list[RunResult]] = {key: [] for key in configured_keys}
    completed_runs = 0

    for _ in range(runs):
        cycle = runner(path=path, only=only)
        completed_runs += 1
        by_key = {result.source_key: result for result in cycle}
        for key in configured_keys:
            result = by_key.get(key)
            if result is not None:
                results_by_source[key].append(result)

    state_after = {config.key: state_loader(config.key) for config in configured}
    source_reports = tuple(
        _evaluate_source(
            config,
            results_by_source[config.key],
            requested_runs=runs,
            state_before=state_before[config.key],
            state_after=state_after[config.key],
            policy=policy,
        )
        for config in configured
    )

    missing_source_keys = tuple(
        source.source_key for source in source_reports if source.run_count != runs
    )
    if missing_source_keys:
        global_reasons.append("missing_source_results")

    if global_reasons or any(source.status == EngineAcceptanceStatus.FAIL.value for source in source_reports):
        status = EngineAcceptanceStatus.FAIL.value
    elif source_reports and all(source.status == EngineAcceptanceStatus.PASS.value for source in source_reports):
        status = EngineAcceptanceStatus.PASS.value
    else:
        status = EngineAcceptanceStatus.NEEDS_EVIDENCE.value

    customer_path_safe = bool(source_reports) and all(source.customer_path_safe for source in source_reports)
    retirement_evidence_complete = bool(source_reports) and all(
        source.retirement_evidence_complete for source in source_reports
    )
    finished_at = datetime.now(UTC)

    return EngineAcceptanceReport(
        schema_version=1,
        task="DP-ENGINE-018",
        scenario="live_engine_acceptance",
        status=status,
        customer_path_safe=customer_path_safe,
        retirement_evidence_complete=retirement_evidence_complete,
        requested_runs=runs,
        completed_runs=completed_runs,
        configured_source_keys=configured_keys,
        missing_source_keys=missing_source_keys,
        source_count=len(source_reports),
        reasons=tuple(dict.fromkeys(global_reasons)),
        policy={
            "min_consecutive_pass_pages": policy.min_consecutive_pass_pages,
            "min_clean_runs": policy.min_clean_runs,
            "sample_every": policy.sample_every,
        },
        sources=source_reports,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=round((finished_at - started_at).total_seconds(), 3),
    )


def write_engine_acceptance_report(
    report: EngineAcceptanceReport,
    output: str | Path = "output/dp_engine_acceptance.json",
) -> Path:
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target
