from __future__ import annotations

import json
import shutil
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from tools.strategy_live_smoke import SmokeSettings, _build_report, _collect_log_excerpt


class StrategyLiveSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_root = Path(__file__).resolve().parents[1] / ".tmp_tests" / uuid4().hex
        self.test_root.mkdir(parents=True, exist_ok=True)
        self.log_file = self.test_root / "runtime.log"

    def tearDown(self) -> None:
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_collect_log_excerpt_filters_on_signal_tokens(self) -> None:
        self.log_file.write_text(
            "\n".join(
                [
                    "2026-04-20 08:00:00 [INFO] x: unrelated line",
                    "2026-04-20 08:00:01 [INFO] x: Strategy order update: signal=TF_ENTER_msg-1 order_id=abc",
                    "2026-04-20 08:00:02 [INFO] x: Strategy order update: signal=TF_STOP_msg-1 order_id=def",
                ]
            ),
            encoding="utf-8",
        )

        excerpt = _collect_log_excerpt(self.log_file, {"msg-1", "TF_STOP_msg-1"})

        self.assertEqual(len(excerpt), 2)
        self.assertTrue(all("msg-1" in line for line in excerpt))

    def test_build_report_includes_relevant_events_and_log_excerpt(self) -> None:
        settings = SmokeSettings(
            report_dir=self.test_root,
            log_file=self.log_file,
            label="unit",
        )
        self.log_file.write_text(
            "2026-04-20 08:00:01 [INFO] core.strategy_bridge: Strategy order update: signal=TF_ENTER_live-smoke-short-1 order_id=abc\n",
            encoding="utf-8",
        )
        events = [
            {
                "event": "ACCEPTED",
                "signal_id": "live-smoke-short-1",
                "timestamp": "2026-04-20T14:00:00+00:00",
                "runtime_state": "Idle",
            },
            {
                "event": "FILLED",
                "signal_id": "live-smoke-short-1",
                "timestamp": "2026-04-20T14:00:01+00:00",
                "runtime_state": "InPosition",
            },
            {
                "event": "ACCEPTED",
                "signal_id": "live-smoke-cleanup-1",
                "timestamp": "2026-04-20T14:00:02+00:00",
                "runtime_state": "InPosition",
            },
        ]

        report = _build_report(
            settings=settings,
            run_started_at=datetime.now(timezone.utc),
            initial_snapshot={"runtime_state": "Idle", "intake_enabled": True},
            final_snapshot={"runtime_state": "Idle", "intake_enabled": True},
            command_signal_id="live-smoke-short-1",
            cleanup_signal_id="live-smoke-cleanup-1",
            heartbeat_ids=["live-smoke-heartbeat-1"],
            events=events,
            status="passed",
            failure_reason="",
            cleanup_sent=True,
            cleanup_event_seen=True,
            filled_seen=True,
            exit_seen=False,
        )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["relevant_event_count"], 3)
        self.assertEqual(report["cleanup_signal_id"], "live-smoke-cleanup-1")
        self.assertTrue(any("TF_ENTER_live-smoke-short-1" in line for line in report["log_excerpt"]))
        json.dumps(report)


if __name__ == "__main__":
    unittest.main()
