from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from src.qa.engine_acceptance import run_engine_acceptance, write_engine_acceptance_report
from src.sources.config import SourceConfig, load_source_configs
from src.sources.parity_telemetry import RetirementMode, SourceParityState
from src.sources.runner import RunResult


def _config(*, runtime_mode: str = "hybrid") -> SourceConfig:
    return SourceConfig(
        key="sample",
        name="Sample",
        adapter="promokood",
        base_url="https://secret.example/path",
        enabled=True,
        runtime_mode=runtime_mode,
    )


def _state(
    *,
    mode: RetirementMode = RetirementMode.OBSERVING,
    observed: int = 0,
    passes: int = 0,
    failures: int = 0,
    consecutive: int = 0,
    clean_runs: int = 0,
) -> SourceParityState:
    return SourceParityState(
        source_key="sample",
        mode=mode,
        parity_observed_pages=observed,
        parity_pass_pages=passes,
        parity_failure_pages=failures,
        consecutive_pass_pages=consecutive,
        clean_runs=clean_runs,
    )


def _result(**overrides) -> RunResult:
    values = {
        "source_key": "sample",
        "fetched": 2,
        "runtime_mode": "hybrid",
        "engine_selected_urls": 2,
        "engine_decoded_pages": 2,
        "engine_generic_pages": 2,
        "engine_legacy_pages": 0,
        "engine_parity_observed_pages": 2,
        "engine_parity_pass_pages": 2,
        "engine_parity_failures": 0,
        "engine_direct_generic_pages": 0,
        "engine_emergency_fallback_pages": 0,
    }
    values.update(overrides)
    return RunResult(**values)


def _state_loader(before: SourceParityState, after: SourceParityState):
    calls = 0

    def load(_: str) -> SourceParityState:
        nonlocal calls
        calls += 1
        return before if calls == 1 else after

    return load


def _runner(result: RunResult):
    def run(**_: object) -> list[RunResult]:
        return [result]

    return run


def test_healthy_observing_cycle_needs_more_retirement_evidence() -> None:
    before = _state()
    after = _state(observed=2, passes=2, consecutive=2, clean_runs=1)

    report = run_engine_acceptance(
        config_loader=lambda _: [_config()],
        state_loader=_state_loader(before, after),
        runner=_runner(_result()),
    )

    assert report.status == "NEEDS_EVIDENCE"
    assert report.customer_path_safe is True
    assert report.retirement_evidence_complete is False
    assert report.sources[0].engine_exercised is True
    assert report.sources[0].reasons == ("retirement_evidence_incomplete",)


def test_generic_primary_with_complete_window_passes() -> None:
    before = _state(mode=RetirementMode.GENERIC_PRIMARY, observed=30, passes=30, consecutive=30, clean_runs=3)
    after = _state(mode=RetirementMode.GENERIC_PRIMARY, observed=30, passes=30, consecutive=30, clean_runs=3)
    result = _result(
        engine_parity_observed_pages=0,
        engine_parity_pass_pages=0,
        engine_direct_generic_pages=2,
    )

    report = run_engine_acceptance(
        config_loader=lambda _: [_config()],
        state_loader=_state_loader(before, after),
        runner=_runner(result),
    )

    assert report.status == "PASS"
    assert report.customer_path_safe is True
    assert report.retirement_evidence_complete is True
    assert report.sources[0].status == "PASS"
    assert report.sources[0].reasons == ()


def test_sampled_parity_mismatch_fails_engine_but_preserves_customer_path_truth() -> None:
    before = _state(mode=RetirementMode.GENERIC_PRIMARY, observed=30, passes=30, consecutive=30, clean_runs=3)
    after = _state(observed=31, passes=30, failures=1, consecutive=0, clean_runs=0)
    result = _result(
        engine_generic_pages=0,
        engine_legacy_pages=1,
        engine_parity_observed_pages=1,
        engine_parity_pass_pages=0,
        engine_parity_failures=1,
    )

    report = run_engine_acceptance(
        config_loader=lambda _: [_config()],
        state_loader=_state_loader(before, after),
        runner=_runner(result),
    )

    source = report.sources[0]
    assert report.status == "FAIL"
    assert source.customer_path_safe is True
    assert "engine_parity_failure" in source.reasons
    assert source.retirement_mode_after == "observing"


def test_emergency_fallback_is_engine_failure_even_when_legacy_returns_data() -> None:
    result = _result(
        engine_generic_pages=0,
        engine_legacy_pages=1,
        engine_parity_failures=1,
        engine_parity_observed_pages=1,
        engine_parity_pass_pages=0,
        engine_emergency_fallback_pages=1,
    )
    report = run_engine_acceptance(
        config_loader=lambda _: [_config()],
        state_loader=_state_loader(_state(), _state(failures=1)),
        runner=_runner(result),
    )

    assert report.status == "FAIL"
    assert report.customer_path_safe is True
    assert "engine_emergency_fallback" in report.sources[0].reasons


def test_zero_fetch_or_collection_error_is_customer_path_failure() -> None:
    result = _result(fetched=0, errors=1, error="do-not-copy-this-error")
    report = run_engine_acceptance(
        config_loader=lambda _: [_config()],
        state_loader=_state_loader(_state(), _state()),
        runner=_runner(result),
    )

    assert report.status == "FAIL"
    assert report.customer_path_safe is False
    assert {"collection_error", "empty_fetch"}.issubset(set(report.sources[0].reasons))


def test_missing_configured_source_result_is_fail() -> None:
    report = run_engine_acceptance(
        config_loader=lambda _: [_config()],
        state_loader=_state_loader(_state(), _state()),
        runner=lambda **_: [],
    )

    assert report.status == "FAIL"
    assert report.missing_source_keys == ("sample",)
    assert "missing_source_results" in report.reasons
    assert "missing_run_result" in report.sources[0].reasons


def test_runtime_fallback_cannot_count_as_engine_acceptance() -> None:
    result = _result(engine_fallback_used=True)
    report = run_engine_acceptance(
        config_loader=lambda _: [_config()],
        state_loader=_state_loader(_state(), _state()),
        runner=_runner(result),
    )

    assert report.status == "FAIL"
    assert "engine_runtime_fallback" in report.sources[0].reasons


def test_non_hybrid_source_is_not_accepted_by_dp018() -> None:
    report = run_engine_acceptance(
        config_loader=lambda _: [_config(runtime_mode="legacy")],
        state_loader=_state_loader(_state(), _state()),
        runner=_runner(_result(runtime_mode="legacy")),
    )

    assert report.status == "FAIL"
    assert "source_not_hybrid" in report.sources[0].reasons


def test_multiple_cycles_are_bounded_and_aggregated() -> None:
    calls = 0

    def runner(**_: object) -> list[RunResult]:
        nonlocal calls
        calls += 1
        return [_result()]

    report = run_engine_acceptance(
        runs=3,
        config_loader=lambda _: [_config()],
        state_loader=_state_loader(
            _state(),
            _state(mode=RetirementMode.GENERIC_PRIMARY, observed=30, passes=30, consecutive=30, clean_runs=3),
        ),
        runner=runner,
    )

    assert calls == 3
    assert report.completed_runs == 3
    assert report.sources[0].run_count == 3
    assert report.sources[0].fetched_total == 6
    assert report.status == "PASS"

    with pytest.raises(ValueError):
        run_engine_acceptance(runs=0, config_loader=lambda _: [])
    with pytest.raises(ValueError):
        run_engine_acceptance(runs=4, config_loader=lambda _: [])


def test_unexercised_generic_path_is_needs_evidence_not_false_pass() -> None:
    result = _result(
        engine_selected_urls=0,
        engine_decoded_pages=0,
        engine_generic_pages=0,
        engine_parity_observed_pages=0,
        engine_parity_pass_pages=0,
    )
    report = run_engine_acceptance(
        config_loader=lambda _: [_config()],
        state_loader=_state_loader(_state(), _state()),
        runner=_runner(result),
    )

    assert report.status == "NEEDS_EVIDENCE"
    assert report.customer_path_safe is True
    assert "generic_engine_not_exercised" in report.sources[0].reasons
    assert "live_parity_not_observed" in report.sources[0].reasons


def test_report_does_not_copy_urls_warnings_errors_or_offer_values() -> None:
    secret = "super-secret-token"
    url = "https://secret.example/private?token=abcd"
    result = _result(
        errors=1,
        error=f"failed {secret} at {url}",
        runtime_warnings=(f"warning {secret} {url}",),
    )
    report = run_engine_acceptance(
        config_loader=lambda _: [_config()],
        state_loader=_state_loader(_state(), _state()),
        runner=_runner(result),
    )
    payload = json.dumps(asdict(report), ensure_ascii=False, default=str)

    assert secret not in payload
    assert url not in payload
    assert "secret.example" not in payload
    assert "runtime_warnings" not in payload
    assert "error" not in asdict(report.sources[0])


def test_atomic_report_writer_round_trips_machine_readable_evidence(tmp_path) -> None:
    report = run_engine_acceptance(
        config_loader=lambda _: [_config()],
        state_loader=_state_loader(_state(), _state(observed=2, passes=2, consecutive=2, clean_runs=1)),
        runner=_runner(_result()),
    )
    target = tmp_path / "nested" / "acceptance.json"

    written = write_engine_acceptance_report(report, target)
    payload = json.loads(written.read_text(encoding="utf-8"))

    assert written == target
    assert payload["task"] == "DP-ENGINE-018"
    assert payload["status"] == "NEEDS_EVIDENCE"
    assert payload["schema_version"] == 1
    assert not list(target.parent.glob("*.tmp"))


def test_shipped_source_baseline_is_five_enabled_hybrid_sources() -> None:
    configs = load_source_configs("config/sources.yaml")
    enabled = [config for config in configs if config.enabled]

    assert {config.key for config in enabled} == {
        "promokood",
        "promokodik",
        "berikod",
        "promokodi_net_ru",
        "promko",
    }
    assert all(config.runtime_mode == "hybrid" for config in enabled)
