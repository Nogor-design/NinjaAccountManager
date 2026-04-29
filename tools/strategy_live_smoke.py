from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = REPO_ROOT / "logs" / "strategy_smoke_reports"
DEFAULT_LOG_FILE = REPO_ROOT / "logs" / "ninja_account_manager.log"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signal_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S%f")[:-3]
    return f"{prefix}-{stamp}"


@dataclass
class SmokeSettings:
    host: str = "127.0.0.1"
    port: int = 8766
    account: str = "Sim101"
    instrument: str = "NQ 06-26"
    side: str = "SHORT"
    quantity: int = 1
    stop_ticks: int = 80
    target_ticks: int = 0
    template: str = "runner_reversal_template"
    thesis_id: str = "strategy_live_smoke"
    entry_timeout: float = 20.0
    hold_seconds: float = 8.0
    heartbeat_seconds: float = 15.0
    cleanup_command: str = "EXIT_ALL"
    report_dir: Path = DEFAULT_REPORT_DIR
    output_path: Path | None = None
    label: str = ""
    log_file: Path = DEFAULT_LOG_FILE


class StrategyAPIClient:
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._sock: socket.socket | None = None
        self._reader = None
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._closed = False
        self._thread: threading.Thread | None = None

    def connect(self) -> dict[str, Any]:
        self._sock = socket.create_connection((self._host, self._port), timeout=5)
        self._reader = self._sock.makefile("r", encoding="utf-8")
        initial = json.loads(self._reader.readline())
        self._thread = threading.Thread(target=self._read_loop, daemon=True, name="StrategyLiveSmokeReader")
        self._thread.start()
        return initial

    def close(self) -> None:
        self._closed = True
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1)

    def send(self, payload: dict[str, Any]) -> None:
        if self._sock is None:
            raise RuntimeError("client not connected")
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            self._sock.sendall(data)

    def drain_events(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._events)
            self._events.clear()
        return items

    def request_snapshot(self, reason: str = "smoke_probe") -> dict[str, Any]:
        payload = {
            "message_type": "COMMAND",
            "command": "HEARTBEAT",
            "signal_id": _signal_id("live-smoke-probe"),
            "timestamp": _now_iso(),
            "account": "",
            "instrument": "",
            "signal_expiry_seconds": 3600,
            "reason": reason,
        }
        self.send(payload)
        deadline = time.monotonic() + 5.0
        latest_snapshot: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            for event in self.drain_events():
                if event.get("event") == "STATE_SNAPSHOT":
                    latest_snapshot = event
                else:
                    with self._lock:
                        self._events.append(event)
            if latest_snapshot is not None:
                return latest_snapshot
            time.sleep(0.05)
        return {}

    def _read_loop(self) -> None:
        assert self._reader is not None
        while not self._closed:
            line = self._reader.readline()
            if not line:
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self._lock:
                self._events.append(event)


def _format_event(event: dict[str, Any]) -> str:
    details = event.get("details") or {}
    message = ""
    if isinstance(details, dict):
        message = str(details.get("message") or details.get("reason") or "")
        exit_reason = str(details.get("exit_reason") or "")
        if exit_reason:
            message = f"{message} ({exit_reason})".strip()
    return (
        f"{event.get('timestamp', '')[:19]} "
        f"{event.get('event', '')} "
        f"signal={event.get('signal_id', '')} "
        f"runtime={event.get('runtime_state', '')} "
        f"error={event.get('error_code', '')} "
        f"{message}"
    ).strip()


def _send_command(client: StrategyAPIClient, command: str, signal_id: str, **kwargs: Any) -> None:
    payload = {
        "message_type": "COMMAND",
        "command": command,
        "signal_id": signal_id,
        "timestamp": _now_iso(),
    }
    payload.update(kwargs)
    client.send(payload)


def _report_path(settings: SmokeSettings, run_started_at: datetime) -> Path:
    if settings.output_path is not None:
        return settings.output_path
    label = settings.label.strip() or settings.side.lower()
    stamp = run_started_at.strftime("%Y%m%d_%H%M%S")
    return settings.report_dir / f"{stamp}_{label}_strategy_smoke.json"


def _collect_log_excerpt(log_file: Path, tokens: set[str]) -> list[str]:
    if not log_file.exists() or not tokens:
        return []
    lines: list[str] = []
    try:
        for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if any(token in line for token in tokens if token):
                lines.append(line)
    except OSError:
        return []
    return lines[-200:]


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run_smoke(settings: SmokeSettings) -> tuple[int, dict[str, Any]]:
    client = StrategyAPIClient(settings.host, settings.port)
    run_started_at = datetime.now(timezone.utc)
    cleanup_signal_id = ""
    heartbeat_ids: list[str] = []
    signal_id = ""
    initial_snapshot: dict[str, Any] = {}
    final_snapshot: dict[str, Any] = {}
    seen: list[dict[str, Any]] = []
    status = "unknown"
    failure_reason = ""
    filled_seen = False
    exit_seen = False
    cleanup_sent = False
    cleanup_event_seen = False

    try:
        initial_snapshot = client.connect()

        if initial_snapshot.get("execution_mode") != "SIM":
            status = "aborted"
            failure_reason = "runtime is not in SIM mode"
            return 2, _build_report(
                settings=settings,
                run_started_at=run_started_at,
                initial_snapshot=initial_snapshot,
                final_snapshot=final_snapshot,
                command_signal_id=signal_id,
                cleanup_signal_id=cleanup_signal_id,
                heartbeat_ids=heartbeat_ids,
                events=seen,
                status=status,
                failure_reason=failure_reason,
            )
        if not initial_snapshot.get("nt_connected"):
            status = "aborted"
            failure_reason = "NinjaTrader is not connected"
            return 2, _build_report(
                settings=settings,
                run_started_at=run_started_at,
                initial_snapshot=initial_snapshot,
                final_snapshot=final_snapshot,
                command_signal_id=signal_id,
                cleanup_signal_id=cleanup_signal_id,
                heartbeat_ids=heartbeat_ids,
                events=seen,
                status=status,
                failure_reason=failure_reason,
            )
        if int(initial_snapshot.get("nt_connection_count") or 0) != 1:
            status = "aborted"
            failure_reason = "expected exactly one NinjaTrader connection"
            return 2, _build_report(
                settings=settings,
                run_started_at=run_started_at,
                initial_snapshot=initial_snapshot,
                final_snapshot=final_snapshot,
                command_signal_id=signal_id,
                cleanup_signal_id=cleanup_signal_id,
                heartbeat_ids=heartbeat_ids,
                events=seen,
                status=status,
                failure_reason=failure_reason,
            )
        if initial_snapshot.get("runtime_state") not in {"Idle", "Disabled"}:
            status = "aborted"
            failure_reason = f"runtime not idle ({initial_snapshot.get('runtime_state')})"
            return 2, _build_report(
                settings=settings,
                run_started_at=run_started_at,
                initial_snapshot=initial_snapshot,
                final_snapshot=final_snapshot,
                command_signal_id=signal_id,
                cleanup_signal_id=cleanup_signal_id,
                heartbeat_ids=heartbeat_ids,
                events=seen,
                status=status,
                failure_reason=failure_reason,
            )
        if not initial_snapshot.get("intake_enabled"):
            status = "aborted"
            failure_reason = "signal intake is disabled; enable intake in the GUI or restart the runtime"
            return 2, _build_report(
                settings=settings,
                run_started_at=run_started_at,
                initial_snapshot=initial_snapshot,
                final_snapshot=final_snapshot,
                command_signal_id=signal_id,
                cleanup_signal_id=cleanup_signal_id,
                heartbeat_ids=heartbeat_ids,
                events=seen,
                status=status,
                failure_reason=failure_reason,
            )

        heartbeat_id = _signal_id("live-smoke-heartbeat")
        heartbeat_ids.append(heartbeat_id)
        _send_command(
            client,
            "HEARTBEAT",
            heartbeat_id,
            account=settings.account,
            instrument=settings.instrument,
            signal_expiry_seconds=3600,
        )
        print(f"Sent HEARTBEAT {heartbeat_id}")

        command = "ENTER_LONG" if settings.side == "LONG" else "ENTER_SHORT"
        signal_id = _signal_id(f"live-smoke-{settings.side.lower()}")
        _send_command(
            client,
            command,
            signal_id,
            account=settings.account,
            instrument=settings.instrument,
            timeframe="1m",
            template_name=settings.template,
            quantity=settings.quantity,
            entry_mode="market",
            stop_ticks=settings.stop_ticks,
            target_ticks=settings.target_ticks,
            thesis_id=settings.thesis_id,
            signal_expiry_seconds=3600,
        )
        print(f"Sent {command} {signal_id}")

        deadline = time.monotonic() + settings.entry_timeout
        last_heartbeat_at = time.monotonic()
        while time.monotonic() < deadline:
            if time.monotonic() - last_heartbeat_at >= settings.heartbeat_seconds:
                hb = _signal_id("live-smoke-heartbeat")
                heartbeat_ids.append(hb)
                _send_command(
                    client,
                    "HEARTBEAT",
                    hb,
                    account=settings.account,
                    instrument=settings.instrument,
                    signal_expiry_seconds=3600,
                )
                print(f"Sent HEARTBEAT {hb}")
                last_heartbeat_at = time.monotonic()

            for event in client.drain_events():
                seen.append(event)
                print(_format_event(event))
                if event.get("signal_id") != signal_id:
                    continue
                if event.get("event") == "FILLED":
                    filled_seen = True
                if event.get("event") == "EXIT_FILLED":
                    exit_seen = True
                    break
            if exit_seen or filled_seen:
                break
            time.sleep(0.25)

        if not filled_seen:
            status = "failed"
            failure_reason = "entry did not fill before timeout"
            cleanup_signal_id = _signal_id("live-smoke-cleanup")
            _send_command(client, settings.cleanup_command, cleanup_signal_id, account=settings.account, instrument=settings.instrument)
            cleanup_sent = True
            time.sleep(2)
            final_snapshot = client.request_snapshot(reason="post_cleanup_timeout")
            return 3, _build_report(
                settings=settings,
                run_started_at=run_started_at,
                initial_snapshot=initial_snapshot,
                final_snapshot=final_snapshot,
                command_signal_id=signal_id,
                cleanup_signal_id=cleanup_signal_id,
                heartbeat_ids=heartbeat_ids,
                events=seen,
                status=status,
                failure_reason=failure_reason,
                cleanup_sent=cleanup_sent,
                cleanup_event_seen=cleanup_event_seen,
                filled_seen=filled_seen,
                exit_seen=exit_seen,
            )

        print(f"Holding for {settings.hold_seconds:.1f}s to observe exit behavior...")
        hold_deadline = time.monotonic() + settings.hold_seconds
        while time.monotonic() < hold_deadline:
            if time.monotonic() - last_heartbeat_at >= settings.heartbeat_seconds:
                hb = _signal_id("live-smoke-heartbeat")
                heartbeat_ids.append(hb)
                _send_command(
                    client,
                    "HEARTBEAT",
                    hb,
                    account=settings.account,
                    instrument=settings.instrument,
                    signal_expiry_seconds=3600,
                )
                print(f"Sent HEARTBEAT {hb}")
                last_heartbeat_at = time.monotonic()

            for event in client.drain_events():
                seen.append(event)
                print(_format_event(event))
                if event.get("signal_id") == signal_id and event.get("event") == "EXIT_FILLED":
                    exit_seen = True
                    break
            if exit_seen:
                break
            time.sleep(0.25)

        cleanup_signal_id = _signal_id("live-smoke-cleanup")
        _send_command(
            client,
            settings.cleanup_command,
            cleanup_signal_id,
            account=settings.account,
            instrument=settings.instrument,
        )
        cleanup_sent = True
        print(f"Sent {settings.cleanup_command} {cleanup_signal_id}")

        final_deadline = time.monotonic() + 10.0
        while time.monotonic() < final_deadline:
            for event in client.drain_events():
                seen.append(event)
                print(_format_event(event))
                if event.get("signal_id") == cleanup_signal_id and event.get("event") in {"ACCEPTED", "EXIT_FILLED"}:
                    cleanup_event_seen = True
            time.sleep(0.25)

        final_snapshot = client.request_snapshot(reason="post_cleanup")
        if exit_seen:
            status = "failed"
            failure_reason = "observed EXIT_FILLED during hold window"
            return 4, _build_report(
                settings=settings,
                run_started_at=run_started_at,
                initial_snapshot=initial_snapshot,
                final_snapshot=final_snapshot,
                command_signal_id=signal_id,
                cleanup_signal_id=cleanup_signal_id,
                heartbeat_ids=heartbeat_ids,
                events=seen,
                status=status,
                failure_reason=failure_reason,
                cleanup_sent=cleanup_sent,
                cleanup_event_seen=cleanup_event_seen,
                filled_seen=filled_seen,
                exit_seen=exit_seen,
            )

        status = "passed"
        return 0, _build_report(
            settings=settings,
            run_started_at=run_started_at,
            initial_snapshot=initial_snapshot,
            final_snapshot=final_snapshot,
            command_signal_id=signal_id,
            cleanup_signal_id=cleanup_signal_id,
            heartbeat_ids=heartbeat_ids,
            events=seen,
            status=status,
            failure_reason=failure_reason,
            cleanup_sent=cleanup_sent,
            cleanup_event_seen=cleanup_event_seen,
            filled_seen=filled_seen,
            exit_seen=exit_seen,
        )
    finally:
        client.close()


def _build_report(
    *,
    settings: SmokeSettings,
    run_started_at: datetime,
    initial_snapshot: dict[str, Any],
    final_snapshot: dict[str, Any],
    command_signal_id: str,
    cleanup_signal_id: str,
    heartbeat_ids: list[str],
    events: list[dict[str, Any]],
    status: str,
    failure_reason: str,
    cleanup_sent: bool = False,
    cleanup_event_seen: bool = False,
    filled_seen: bool = False,
    exit_seen: bool = False,
) -> dict[str, Any]:
    run_finished_at = datetime.now(timezone.utc)
    tracked_tokens = {
        command_signal_id,
        cleanup_signal_id,
        *(heartbeat_ids or []),
        f"TF_ENTER_{command_signal_id}" if command_signal_id else "",
        f"TF_STOP_{command_signal_id}" if command_signal_id else "",
        f"TF_TARGET_{command_signal_id}" if command_signal_id else "",
        f"TF_EXIT_{cleanup_signal_id}" if cleanup_signal_id else "",
    }
    relevant_events = [
        event
        for event in events
        if event.get("signal_id") in {command_signal_id, cleanup_signal_id, *heartbeat_ids}
    ]
    report = {
        "status": status,
        "failure_reason": failure_reason,
        "run_started_at_utc": run_started_at.isoformat(),
        "run_finished_at_utc": run_finished_at.isoformat(),
        "settings": {
            **asdict(settings),
            "report_dir": str(settings.report_dir),
            "output_path": str(settings.output_path) if settings.output_path is not None else "",
            "log_file": str(settings.log_file),
        },
        "initial_snapshot": initial_snapshot,
        "final_snapshot": final_snapshot,
        "command_signal_id": command_signal_id,
        "cleanup_signal_id": cleanup_signal_id,
        "heartbeat_ids": list(heartbeat_ids),
        "filled_seen": filled_seen,
        "exit_seen_during_hold": exit_seen,
        "cleanup_sent": cleanup_sent,
        "cleanup_event_seen": cleanup_event_seen,
        "event_count": len(events),
        "relevant_event_count": len(relevant_events),
        "events": events,
        "relevant_events": relevant_events,
        "log_excerpt": _collect_log_excerpt(settings.log_file, {token for token in tracked_tokens if token}),
    }
    return report


def parse_args() -> SmokeSettings:
    parser = argparse.ArgumentParser(description="Drive a live local smoke test against NinjaAccountManager.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--account", default="Sim101")
    parser.add_argument("--instrument", default="NQ 06-26")
    parser.add_argument("--side", choices=["LONG", "SHORT"], default="SHORT")
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--stop-ticks", type=int, default=80)
    parser.add_argument("--target-ticks", type=int, default=0)
    parser.add_argument("--template", default="runner_reversal_template")
    parser.add_argument("--thesis-id", default="strategy_live_smoke")
    parser.add_argument("--entry-timeout", type=float, default=20.0)
    parser.add_argument("--hold-seconds", type=float, default=8.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=15.0)
    parser.add_argument(
        "--cleanup-command",
        choices=["EXIT_ALL", "SCRATCH", "FLATTEN_AND_DISABLE"],
        default="EXIT_ALL",
    )
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--output", default="")
    parser.add_argument("--label", default="")
    parser.add_argument("--log-file", default=str(DEFAULT_LOG_FILE))
    args = parser.parse_args()
    return SmokeSettings(
        host=args.host,
        port=args.port,
        account=args.account,
        instrument=args.instrument,
        side=args.side,
        quantity=args.quantity,
        stop_ticks=args.stop_ticks,
        target_ticks=args.target_ticks,
        template=args.template,
        thesis_id=args.thesis_id,
        entry_timeout=args.entry_timeout,
        hold_seconds=args.hold_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
        cleanup_command=args.cleanup_command,
        report_dir=Path(args.report_dir),
        output_path=Path(args.output) if args.output else None,
        label=args.label,
        log_file=Path(args.log_file),
    )


def main() -> int:
    settings = parse_args()
    code, report = run_smoke(settings)
    print("Initial snapshot:")
    print(json.dumps(report.get("initial_snapshot", {}), indent=2))
    relevant = report.get("relevant_events") or []
    if relevant:
        print("\nRelevant sequence:")
        for event in relevant:
            print(_format_event(event))
    report_path = _report_path(settings, datetime.fromisoformat(report["run_started_at_utc"]))
    _write_report(report_path, report)
    print(f"\nReport written to {report_path}")
    if code == 0:
        print("\nNo unexpected EXIT_FILLED observed during hold window.")
    else:
        print(f"\nSmoke test failed: {report.get('failure_reason') or report.get('status')}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
