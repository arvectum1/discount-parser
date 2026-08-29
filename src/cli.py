from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from src.jobs.lifecycle import maintenance
from src.jobs.scheduler import run_scheduler
from src.modules.source_registry.discovery import discover_merchant_pages
from src.modules.source_registry.runner import collect_registered_sources
from src.modules.source_registry.seed import seed_registry
from src.modules.source_registry.xlsx import export_source_registry_xlsx, import_source_registry_xlsx
from src.qa.doctor import build_doctor_report
from src.qa.engine_acceptance import run_engine_acceptance, write_engine_acceptance_report
from src.qa.report import write_smoke_report
from src.runtime import run_all as run_runtime
from src.shared.config import get_settings
from src.shared.db import session_scope
from src.sources.parity_telemetry import parity_report
from src.sources.runner import run_all
from src.telegram.runner import run_bot
from src.web.launcher import run_web_panel


def _configure_console_encoding() -> None:
    """Use UTF-8 for Russian diagnostic output on Windows and frozen workers."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="discount-parser")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse_cmd = subparsers.add_parser("parse", help="Collect configured discount sources")
    parse_cmd.add_argument("--source", default=None, help="Run only one legacy source key")
    parse_cmd.add_argument("--config", default=None, help="Path to sources YAML")

    registry_collect = subparsers.add_parser("registry-collect", help="Collect persisted non-legacy registered sources")
    registry_collect.add_argument("--source", default=None, help="Run only one registered source key")

    subparsers.add_parser("registry-seed", help="Seed default keywords and mirror legacy source adapters into the registry")

    registry_export = subparsers.add_parser("registry-export", help="Export source registry/candidates/keywords to XLSX")
    registry_export.add_argument("--output", default="output/sources_registry.xlsx")

    registry_import = subparsers.add_parser("registry-import", help="Import source registry/candidates/keywords from XLSX")
    registry_import.add_argument("path")

    discover = subparsers.add_parser("discover-merchant", help="Discover same-domain promotion page candidates")
    discover.add_argument("--merchant", required=True)
    discover.add_argument("--url", required=True)
    discover.add_argument("--max-candidates", type=int, default=20)

    subparsers.add_parser("parity-report", help="Show DP Engine live source parity and retirement state")

    engine_acceptance = subparsers.add_parser(
        "engine-acceptance",
        help="Run bounded live DP Engine acceptance and write privacy-safe evidence",
    )
    engine_acceptance.add_argument("--source", default=None, help="Evaluate only one configured source key")
    engine_acceptance.add_argument("--config", default=None, help="Path to sources YAML")
    engine_acceptance.add_argument(
        "--runs",
        type=int,
        choices=(1, 2, 3),
        default=1,
        help="Production collection cycles to execute (max 3)",
    )
    engine_acceptance.add_argument(
        "--output",
        default="output/dp_engine_acceptance.json",
        help="Destination JSON evidence path",
    )

    subparsers.add_parser("maintenance", help="Expire and review stale offers")
    subparsers.add_parser("scheduler", help="Run collection, maintenance and autopost scheduler")
    subparsers.add_parser("bot", help="Run Telegram control bot")
    subparsers.add_parser("run", help="Run Telegram bot and scheduler together")
    subparsers.add_parser("web", help="Open local web control panel")
    subparsers.add_parser("doctor", help="Run local preflight checks before live testing")

    report_cmd = subparsers.add_parser("smoke-report", help="Write JSON delivery evidence from the current database")
    report_cmd.add_argument("--output", default="output/smoke_report.json", help="Destination JSON path")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_console_encoding()
    args = build_parser().parse_args(argv)
    settings = get_settings()

    if args.command == "parse":
        results = run_all(path=args.config or settings.sources_config_path, only=args.source)
        print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2, default=str))
        return 1 if any(result.errors and result.fetched == 0 for result in results) else 0

    if args.command == "registry-seed":
        with session_scope() as session:
            result = seed_registry(session, sources_config_path=settings.sources_config_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "registry-collect":
        results = collect_registered_sources(only_key=args.source)
        print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2, default=str))
        return 1 if any(result.errors and result.fetched == 0 for result in results) else 0

    if args.command == "registry-export":
        path = export_source_registry_xlsx(args.output)
        print(path)
        return 0

    if args.command == "registry-import":
        report = import_source_registry_xlsx(args.path)
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return 1 if report.errors else 0

    if args.command == "discover-merchant":
        result = discover_merchant_pages(
            merchant=args.merchant,
            homepage=args.url,
            max_candidates=max(1, min(args.max_candidates, 100)),
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
        return 1 if result.error else 0

    if args.command == "parity-report":
        print(json.dumps([asdict(row) for row in parity_report()], ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "engine-acceptance":
        report = run_engine_acceptance(
            path=args.config or settings.sources_config_path,
            only=args.source,
            runs=args.runs,
        )
        evidence_path = write_engine_acceptance_report(report, args.output)
        payload = asdict(report)
        payload["evidence_path"] = str(evidence_path)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 1 if report.status == "FAIL" else 0

    if args.command == "maintenance":
        result = maintenance(stale_after_days=settings.stale_after_days)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "scheduler":
        run_scheduler()
        return 0

    if args.command == "bot":
        run_bot()
        return 0

    if args.command == "run":
        run_runtime()
        return 0

    if args.command == "web":
        run_web_panel()
        return 0

    if args.command == "doctor":
        report = build_doctor_report()
        print(report.to_json())
        return 0 if report.ok else 1

    if args.command == "smoke-report":
        path = write_smoke_report(args.output)
        print(path)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
