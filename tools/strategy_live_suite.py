from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.strategy_live_smoke import SmokeSettings, _report_path, _write_report, run_smoke


DEFAULT_SUITE_REPORT_DIR = Path(__file__).resolve().parents[1] / "logs" / "strategy_smoke_reports"


def _suite_report_name(label: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = label.strip() or "both"
    return f"{stamp}_{suffix}_strategy_suite.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run back-to-back live strategy smoke scenarios.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--account", default="Sim101")
    parser.add_argument("--instrument", default="NQ 06-26")
    parser.add_argument("--sides", choices=["LONG", "SHORT", "BOTH"], default="BOTH")
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--stop-ticks", type=int, default=80)
    parser.add_argument("--target-ticks", type=int, default=0)
    parser.add_argument("--template", default="runner_reversal_template")
    parser.add_argument("--thesis-id", default="strategy_live_suite")
    parser.add_argument("--entry-timeout", type=float, default=20.0)
    parser.add_argument("--hold-seconds", type=float, default=8.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=15.0)
    parser.add_argument("--cleanup-command", choices=["EXIT_ALL", "SCRATCH", "FLATTEN_AND_DISABLE"], default="EXIT_ALL")
    parser.add_argument("--pause-seconds", type=float, default=3.0)
    parser.add_argument("--report-dir", default=str(DEFAULT_SUITE_REPORT_DIR))
    parser.add_argument("--log-file", default=str(Path(__file__).resolve().parents[1] / "logs" / "ninja_account_manager.log"))
    args = parser.parse_args()

    sides = ["LONG", "SHORT"] if args.sides == "BOTH" else [args.sides]
    reports: list[dict] = []
    overall_code = 0
    started = datetime.now(timezone.utc)

    for index, side in enumerate(sides, start=1):
        print(f"\n=== Running {side} smoke ({index}/{len(sides)}) ===")
        settings = SmokeSettings(
            host=args.host,
            port=args.port,
            account=args.account,
            instrument=args.instrument,
            side=side,
            quantity=args.quantity,
            stop_ticks=args.stop_ticks,
            target_ticks=args.target_ticks,
            template=args.template,
            thesis_id=f"{args.thesis_id}_{side.lower()}",
            entry_timeout=args.entry_timeout,
            hold_seconds=args.hold_seconds,
            heartbeat_seconds=args.heartbeat_seconds,
            cleanup_command=args.cleanup_command,
            report_dir=Path(args.report_dir),
            output_path=None,
            label=f"suite_{side.lower()}",
            log_file=Path(args.log_file),
        )
        code, report = run_smoke(settings)
        _write_report(_report_path(settings, datetime.fromisoformat(report["run_started_at_utc"])), report)
        reports.append(report)
        overall_code = max(overall_code, code)
        if code != 0:
            print(f"{side} smoke failed: {report.get('failure_reason') or report.get('status')}")
            break
        if index < len(sides):
            time.sleep(max(1.0, args.pause_seconds))

    suite_report = {
        "run_started_at_utc": started.isoformat(),
        "run_finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "overall_code": overall_code,
        "scenario_count": len(reports),
        "reports": reports,
    }
    report_path = Path(args.report_dir) / _suite_report_name(args.sides.lower())
    _write_report(report_path, suite_report)
    print(f"\nSuite report written to {report_path}")
    print(json.dumps(
        {
            "overall_code": overall_code,
            "scenario_statuses": [report.get("status") for report in reports],
            "scenario_failures": [report.get("failure_reason") for report in reports if report.get("failure_reason")],
        },
        indent=2,
    ))
    return overall_code


if __name__ == "__main__":
    raise SystemExit(main())
