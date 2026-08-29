from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from src import cli
from src.qa.engine_acceptance import EngineAcceptanceReport


def _report(status: str) -> EngineAcceptanceReport:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return EngineAcceptanceReport(
        schema_version=1,
        task="DP-ENGINE-018",
        scenario="live_engine_acceptance",
        status=status,
        customer_path_safe=status != "FAIL",
        retirement_evidence_complete=status == "PASS",
        requested_runs=1,
        completed_runs=1,
        configured_source_keys=(),
        missing_source_keys=(),
        source_count=0,
        reasons=(),
        policy={"min_consecutive_pass_pages": 30, "min_clean_runs": 3, "sample_every": 10},
        sources=(),
        started_at=now,
        finished_at=now,
        duration_seconds=0.0,
    )


def test_engine_acceptance_parser_defaults_and_bounds() -> None:
    args = cli.build_parser().parse_args(["engine-acceptance"])
    assert args.command == "engine-acceptance"
    assert args.runs == 1
    assert args.output == "output/dp_engine_acceptance.json"

    args = cli.build_parser().parse_args(["engine-acceptance", "--runs", "3", "--source", "promokood"])
    assert args.runs == 3
    assert args.source == "promokood"


def test_needs_evidence_is_non_failing_but_fail_returns_nonzero(monkeypatch, tmp_path: Path) -> None:
    class Settings:
        sources_config_path = "config/sources.yaml"

    monkeypatch.setattr(cli, "get_settings", lambda: Settings())
    monkeypatch.setattr(cli, "write_engine_acceptance_report", lambda report, output: Path(output))

    monkeypatch.setattr(cli, "run_engine_acceptance", lambda **_: _report("NEEDS_EVIDENCE"))
    assert cli.main(["engine-acceptance", "--output", str(tmp_path / "needs.json")]) == 0

    monkeypatch.setattr(cli, "run_engine_acceptance", lambda **_: _report("FAIL"))
    assert cli.main(["engine-acceptance", "--output", str(tmp_path / "fail.json")]) == 1
