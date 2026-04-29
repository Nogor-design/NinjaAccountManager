from __future__ import annotations

import json
import shutil
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from core.config import AppConfig
from core.data_models import Order, Position
from core.event_bus import EventBus, Events
from core.strategy_bridge import StrategyBridgeService


class FakeNinjaTraderClient:
    def __init__(self) -> None:
        self.submit_calls: list[dict] = []
        self.cancel_calls: list[str] = []
        self._connected = True
        self._connection_count = 1

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def connection_count(self) -> int:
        return self._connection_count

    def submit_order(self, **kwargs) -> None:
        self.submit_calls.append(kwargs)

    def cancel_order(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)


class StrategyBridgeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_root = Path(__file__).resolve().parents[1] / ".tmp_tests" / uuid4().hex
        self.bridge_root = self.test_root / "bridge"
        self.bus = EventBus()
        self.client = FakeNinjaTraderClient()
        self.config = AppConfig()
        self.config.legacy_file_bridge_enabled = True
        self.config.bridge_root = self.bridge_root
        self.config.bridge_state_flush_seconds = 0.0
        self.config.bridge_poll_interval_seconds = 0.01
        self.service = StrategyBridgeService(self.config, self.bus, self.client)  # type: ignore[arg-type]
        self.strategy_events: list[dict] = []
        self.bus.subscribe(Events.STRATEGY_EVENT, self.strategy_events.append)
        self.service._ensure_directories()
        self.service._subscribe()

    def tearDown(self) -> None:
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_entry_signal_is_accepted_and_archived(self) -> None:
        self._write_instruction(
            "enter-1.json",
            {
                "message_id": "msg-1",
                "timestamp": self._now_iso(),
                "instrument": "NQ 06-26",
                "timeframe": "1m",
                "action": "ENTER_LONG",
                "side": "LONG",
                "template_name": "runner_reversal_template",
                "quantity": 1,
                "stop_ticks": 8,
                "target_ticks": 8,
                "thesis_id": "thesis-1",
                "signal_expiry_seconds": 3600,
            },
        )

        self.service.process_inbox_once()

        self.assertEqual(len(self.client.submit_calls), 1)
        first_call = self.client.submit_calls[0]
        self.assertEqual(first_call["signal_name"], "TF_ENTER_msg-1")
        self.assertTrue((self.bridge_root / "archive" / "enter-1.json").exists())
        self.assertFalse((self.bridge_root / "rejected" / "enter-1.json").exists())

        statuses = self._outbox_statuses()
        self.assertIn("ACCEPTED", statuses)
        self.assertIn("ENTRY_SUBMITTED", statuses)

        state = self._read_state()
        self.assertEqual(state["ShellMode"], "EntryPending")
        self.assertEqual(state["PositionId"], "msg-1")

    def test_duplicate_message_id_is_rejected(self) -> None:
        payload = {
            "message_id": "msg-dup",
            "timestamp": self._now_iso(),
            "instrument": "NQ 06-26",
            "timeframe": "1m",
            "action": "ENTER_LONG",
            "side": "LONG",
            "template_name": "runner_reversal_template",
            "quantity": 1,
            "stop_ticks": 8,
            "target_ticks": 8,
            "thesis_id": "thesis-dup",
            "signal_expiry_seconds": 3600,
        }
        self._write_instruction("first.json", payload)
        self.service.process_inbox_once()

        self._write_instruction("second.json", payload)
        self.service.process_inbox_once()

        self.assertEqual(len(self.client.submit_calls), 1)
        self.assertTrue((self.bridge_root / "rejected" / "second.json").exists())
        rejected = [event for event in self._outbox_events() if event["status"] == "REJECTED"]
        self.assertTrue(any("duplicate message_id" in event["detail"] for event in rejected))

    def test_entry_fill_attaches_stop_and_target(self) -> None:
        self._write_instruction(
            "enter-fill.json",
            {
                "message_id": "msg-fill",
                "timestamp": self._now_iso(),
                "instrument": "NQ 06-26",
                "timeframe": "1m",
                "action": "ENTER_LONG",
                "side": "LONG",
                "template_name": "runner_reversal_template",
                "quantity": 1,
                "stop_ticks": 8,
                "target_ticks": 8,
                "thesis_id": "thesis-fill",
                "signal_expiry_seconds": 3600,
            },
        )
        self.service.process_inbox_once()

        self.bus.publish(
            Events.ORDER_UPDATE,
            Order(
                order_id="entry-1",
                account="Sim101",
                instrument="NQ 06-26",
                signal_name="TF_ENTER_msg-fill",
                action="Buy",
                order_type="Market",
                quantity=1,
                filled_quantity=1,
                price=0.0,
                status="Filled",
            ),
        )
        self.bus.publish(
            Events.POSITION_UPDATE,
            Position(
                account="Sim101",
                instrument="NQ 06-26",
                quantity=1,
                avg_price=100.0,
                unrealized_pnl=0.0,
                market_value=100.0,
            ),
        )
        self.service.process_inbox_once()

        self.assertEqual(len(self.client.submit_calls), 3)
        stop_call = self.client.submit_calls[1]
        target_call = self.client.submit_calls[2]
        self.assertEqual(stop_call["signal_name"], "TF_STOP_msg-fill")
        self.assertEqual(stop_call["order_type"], "Stop")
        self.assertAlmostEqual(stop_call["stop_price"], 98.0)
        self.assertEqual(target_call["signal_name"], "TF_TARGET_msg-fill")
        self.assertEqual(target_call["order_type"], "Limit")
        self.assertAlmostEqual(target_call["price"], 102.0)

        statuses = self._outbox_statuses()
        self.assertIn("FILLED", statuses)
        self.assertIn("STOP_ATTACHED", statuses)

        state = self._read_state()
        self.assertEqual(state["ShellMode"], "InPosition")

    def test_entry_is_rejected_when_multiple_nt_connections_exist(self) -> None:
        self.client._connection_count = 2
        self._write_instruction(
            "enter-multi.json",
            {
                "message_id": "msg-multi",
                "timestamp": self._now_iso(),
                "instrument": "NQ 06-26",
                "timeframe": "1m",
                "action": "ENTER_LONG",
                "side": "LONG",
                "template_name": "runner_reversal_template",
                "quantity": 1,
                "stop_ticks": 8,
                "target_ticks": 8,
                "thesis_id": "thesis-multi",
                "signal_expiry_seconds": 3600,
            },
        )

        self.service.process_inbox_once()

        self.assertEqual(len(self.client.submit_calls), 0)
        self.assertTrue((self.bridge_root / "rejected" / "enter-multi.json").exists())
        rejected = [event for event in self._outbox_events() if event["status"] == "REJECTED"]
        self.assertTrue(any("expected exactly one ninjatrader connection" in event["detail"] for event in rejected))

    def test_live_mode_requires_confirmation_before_intake_enable(self) -> None:
        live_config = AppConfig()
        live_config.execution_mode = "LIVE"
        live_config.legacy_file_bridge_enabled = False
        live_service = StrategyBridgeService(live_config, self.bus, self.client)  # type: ignore[arg-type]

        rejected = live_service.set_intake_enabled(True, confirmed_live=False, source="test")
        accepted = live_service.set_intake_enabled(True, confirmed_live=True, source="test")

        self.assertFalse(rejected)
        self.assertTrue(accepted)
        snapshot = live_service.build_state_snapshot(reason="test")
        self.assertEqual(snapshot["execution_mode"], "LIVE")
        self.assertTrue(snapshot["live_confirmation_received"])

    def test_clear_recovery_requires_flat_position(self) -> None:
        self.service._recovery_required = True
        self.service._recovery_reason = "live_position_detected_without_runtime_context"
        self.service._shell_mode = "RecoveryRequired"
        self.service._positions[("Sim101", "NQ 06-26")] = Position(
            account="Sim101",
            instrument="NQ 06-26",
            quantity=1,
            avg_price=100.0,
            unrealized_pnl=0.0,
            market_value=100.0,
        )

        rejected = self.service.clear_recovery_if_flat(source="test")
        self.service._positions[("Sim101", "NQ 06-26")] = Position(
            account="Sim101",
            instrument="NQ 06-26",
            quantity=0,
            avg_price=0.0,
            unrealized_pnl=0.0,
            market_value=0.0,
        )
        accepted = self.service.clear_recovery_if_flat(source="test")

        self.assertFalse(rejected)
        self.assertTrue(accepted)
        self.assertFalse(self.service.build_state_snapshot(reason="test")["recovery_required"])

    def test_unmapped_old_exit_fill_is_ignored_for_current_active_trade(self) -> None:
        self._write_instruction(
            "enter-active.json",
            {
                "message_id": "msg-active",
                "timestamp": self._now_iso(),
                "instrument": "NQ 06-26",
                "timeframe": "1m",
                "action": "ENTER_SHORT",
                "side": "SHORT",
                "template_name": "runner_reversal_template",
                "quantity": 1,
                "stop_ticks": 8,
                "target_ticks": 8,
                "thesis_id": "thesis-active",
                "signal_expiry_seconds": 3600,
            },
        )
        self.service.process_inbox_once()

        self.bus.publish(
            Events.ORDER_UPDATE,
            Order(
                order_id="old-exit-1",
                account="Sim101",
                instrument="NQ 06-26",
                signal_name="TF_EXIT_old_instruction",
                action="Buy",
                order_type="Market",
                quantity=1,
                filled_quantity=1,
                price=101.0,
                status="Filled",
            ),
        )

        exit_events = [event for event in self.strategy_events if event.get("event") == "EXIT_FILLED"]
        self.assertEqual(exit_events, [])

    def test_stop_only_protective_mode_does_not_raise_missing_target_fault(self) -> None:
        self._write_instruction(
            "enter-stop-only.json",
            {
                "message_id": "msg-stop-only",
                "timestamp": self._now_iso(),
                "instrument": "NQ 06-26",
                "timeframe": "1m",
                "action": "ENTER_SHORT",
                "side": "SHORT",
                "template_name": "runner_reversal_template",
                "quantity": 1,
                "stop_ticks": 8,
                "target_ticks": 0,
                "thesis_id": "thesis-stop-only",
                "signal_expiry_seconds": 3600,
            },
        )
        self.service.process_inbox_once()

        self.bus.publish(
            Events.ORDER_UPDATE,
            Order(
                order_id="entry-stop-only",
                account="Sim101",
                instrument="NQ 06-26",
                signal_name="TF_ENTER_msg-stop-only",
                action="Sell",
                order_type="Market",
                quantity=1,
                filled_quantity=1,
                status="Filled",
            ),
        )
        self.bus.publish(
            Events.POSITION_UPDATE,
            Position(
                account="Sim101",
                instrument="NQ 06-26",
                quantity=-1,
                avg_price=100.0,
                unrealized_pnl=0.0,
                market_value=100.0,
            ),
        )
        self.bus.publish(
            Events.ORDER_UPDATE,
            Order(
                order_id="stop-only-1",
                account="Sim101",
                instrument="NQ 06-26",
                signal_name="TF_STOP_msg-stop-only",
                action="Buy",
                order_type="StopMarket",
                quantity=1,
                filled_quantity=0,
                stop_price=102.0,
                status="Working",
            ),
        )
        self.service.process_inbox_once()

        errors = [
            event for event in self.strategy_events
            if event.get("event") == "ERROR" and event.get("signal_id") == "msg-stop-only"
        ]
        self.assertEqual(errors, [])

    def test_lingering_protective_order_is_retried_after_flat(self) -> None:
        self._write_instruction(
            "enter-cleanup.json",
            {
                "message_id": "msg-cleanup",
                "timestamp": self._now_iso(),
                "instrument": "NQ 06-26",
                "timeframe": "1m",
                "action": "ENTER_LONG",
                "side": "LONG",
                "template_name": "runner_reversal_template",
                "quantity": 1,
                "stop_ticks": 8,
                "target_ticks": 0,
                "thesis_id": "thesis-cleanup",
                "signal_expiry_seconds": 3600,
            },
        )
        self.service.process_inbox_once()

        self.bus.publish(
            Events.ORDER_UPDATE,
            Order(
                order_id="entry-cleanup",
                account="Sim101",
                instrument="NQ 06-26",
                signal_name="TF_ENTER_msg-cleanup",
                action="Buy",
                order_type="Market",
                quantity=1,
                filled_quantity=1,
                status="Filled",
            ),
        )
        self.bus.publish(
            Events.POSITION_UPDATE,
            Position(
                account="Sim101",
                instrument="NQ 06-26",
                quantity=1,
                avg_price=100.0,
                unrealized_pnl=0.0,
                market_value=100.0,
            ),
        )
        self.bus.publish(
            Events.ORDER_UPDATE,
            Order(
                order_id="stop-cleanup",
                account="Sim101",
                instrument="NQ 06-26",
                signal_name="TF_STOP_msg-cleanup",
                action="Sell",
                order_type="StopMarket",
                quantity=1,
                filled_quantity=0,
                stop_price=98.0,
                status="Working",
            ),
        )

        self.service.submit_operator_command(
            {
                "command": "EXIT_ALL",
                "signal_id": "exit-cleanup",
                "timestamp": self._now_iso(),
                "account": "Sim101",
                "instrument": "NQ 06-26",
            },
            source="test",
        )
        self.bus.publish(
            Events.POSITION_UPDATE,
            Position(
                account="Sim101",
                instrument="NQ 06-26",
                quantity=0,
                avg_price=100.0,
                unrealized_pnl=0.0,
                market_value=0.0,
            ),
        )
        self.service.process_inbox_once()

        self.assertGreaterEqual(self.client.cancel_calls.count("stop-cleanup"), 2)
        cleanup_errors = [
            event for event in self.strategy_events
            if event.get("error_code") == "protective_cleanup_pending"
        ]
        self.assertTrue(cleanup_errors)

    def _write_instruction(self, name: str, payload: dict) -> None:
        path = self.bridge_root / "inbox" / name
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _outbox_events(self) -> list[dict]:
        events: list[dict] = []
        for path in sorted((self.bridge_root / "outbox").glob("*.evt.json")):
            events.append(json.loads(path.read_text(encoding="utf-8")))
        return events

    def _outbox_statuses(self) -> list[str]:
        return [event["status"] for event in self._outbox_events()]

    def _read_state(self) -> dict:
        return json.loads((self.bridge_root / "state" / "shell_state.json").read_text(encoding="utf-8"))

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    unittest.main()
