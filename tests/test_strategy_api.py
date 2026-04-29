from __future__ import annotations

import json
import socket
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
import shutil
from uuid import uuid4

from core.config import AppConfig
from core.data_models import Order, Position
from core.event_bus import EventBus, Events
from core.strategy_api import StrategyAPIServer
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


class StrategyAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = EventBus()
        self.client_statuses: list[dict] = []
        self.bus.subscribe(Events.STRATEGY_CLIENT_STATUS, self.client_statuses.append)
        self.client = FakeNinjaTraderClient()
        self.config = AppConfig()
        self.config.legacy_file_bridge_enabled = False
        self.config.strategy_api_port = self._find_free_port()
        self.config.bridge_root = Path(__file__).resolve().parents[1] / ".tmp_tests" / uuid4().hex / "bridge"
        self.runtime = StrategyBridgeService(self.config, self.bus, self.client)  # type: ignore[arg-type]
        self.runtime.start()
        self.api = StrategyAPIServer(self.config, self.bus, self.runtime)
        self.api.start()

    def tearDown(self) -> None:
        self.api.stop()
        self.runtime.stop()
        shutil.rmtree(self.config.bridge_root.parent, ignore_errors=True)

    def test_direct_command_submission_and_fill_events(self) -> None:
        with socket.create_connection((self.config.strategy_api_host, self.config.strategy_api_port), timeout=2) as conn:
            reader = conn.makefile("r", encoding="utf-8")
            initial = json.loads(reader.readline())
            self.assertEqual(initial["event"], "STATE_SNAPSHOT")
            self.assertIn("execution_mode", initial)
            self.assertIn("protective_orders_faulted", initial)
            self.assertEqual(initial["nt_connection_count"], 1)

            payload = {
                "message_type": "COMMAND",
                "command": "ENTER_LONG",
                "signal_id": "socket-msg-1",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "account": "Sim101",
                "instrument": "NQ 06-26",
                "timeframe": "1m",
                "template_name": "runner_reversal_template",
                "quantity": 1,
                "entry_mode": "market",
                "stop_ticks": 8,
                "target_ticks": 8,
                "thesis_id": "socket-thesis-1",
                "signal_expiry_seconds": 3600,
            }
            conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))

            accepted = self._read_until_event(reader, "ACCEPTED")
            entry_submitted = self._read_until_event(reader, "ENTRY_SUBMITTED")
            self.assertEqual(accepted["signal_id"], "socket-msg-1")
            self.assertEqual(entry_submitted["signal_id"], "socket-msg-1")
            self.assertEqual(len(self.client.submit_calls), 1)

            self.bus.publish(
                Events.ORDER_UPDATE,
                Order(
                    order_id="entry-socket-1",
                    account="Sim101",
                    instrument="NQ 06-26",
                    signal_name="TF_ENTER_socket-msg-1",
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

            filled = self._read_until_event(reader, "FILLED")
            stop_attached = self._read_until_event(reader, "STOP_ATTACHED")
            target_attached = self._read_until_event(reader, "TARGET_ATTACHED")
            self.assertEqual(filled["signal_id"], "socket-msg-1")
            self.assertEqual(stop_attached["event"], "STOP_ATTACHED")
            self.assertEqual(target_attached["event"], "TARGET_ATTACHED")
            self.assertTrue(any(status["client_count"] >= 1 for status in self.client_statuses))

    def _read_until_event(self, reader, event_name: str, timeout_seconds: float = 2.0) -> dict:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            line = reader.readline()
            if not line:
                break
            event = json.loads(line)
            if event.get("event") == event_name:
                return event
        raise AssertionError(f"Did not receive event {event_name!r}")

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])


if __name__ == "__main__":
    unittest.main()
