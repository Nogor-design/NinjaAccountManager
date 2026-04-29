"""
Thread-safe application state store.

Background threads (WebSocket callbacks) write here; the GUI main-thread reads
here every render frame. All public methods acquire an RLock so callers never
need to synchronize themselves.
"""
from __future__ import annotations

import threading
from collections import deque

from core.data_models import (
    AccountData,
    Candle,
    MarketData,
    Order,
    Position,
    RuntimeHealthState,
    StrategyEventRecord,
    StrategySnapshot,
)


class AppState:
    def __init__(
        self,
        max_log_lines: int = 500,
        max_candles: int = 200,
        max_strategy_events: int = 300,
    ) -> None:
        self._lock = threading.RLock()
        self._max_candles = max_candles
        self._max_log_lines = max_log_lines

        # Trading data
        self.accounts: dict[str, AccountData] = {}
        self.positions: dict[str, Position] = {}   # key: "account:instrument"
        self.orders: dict[str, Order] = {}         # key: order_id
        self.market_data: dict[str, MarketData] = {}
        self.candles: dict[str, list[Candle]] = {}  # key: instrument

        # Connection state
        self.is_server_running: bool = False
        self.nt_connected: bool = False

        # Log ring buffer
        self.log_lines: deque[str] = deque(maxlen=max_log_lines)

        # Strategy runtime state
        self.strategy_snapshot: StrategySnapshot | None = None
        self.strategy_health = RuntimeHealthState()
        self.strategy_events: deque[StrategyEventRecord] = deque(maxlen=max_strategy_events)
        self.strategy_connected_clients: int = 0
        self.strategy_api_listening: bool = False
        self.strategy_api_endpoint: str = ""
        self.strategy_last_event_at: str = ""
        self.strategy_last_heartbeat_at: str = ""
        self.strategy_mode: str = "Idle"
        self.strategy_fault: str = ""

        # Dirty flags set by writers and cleared by the GUI.
        self.accounts_dirty: bool = False
        self.positions_dirty: bool = False
        self.orders_dirty: bool = False
        self.market_data_dirty: bool = False
        self.logs_dirty: bool = False
        self.candles_dirty: dict[str, bool] = {}
        self.strategy_dirty: bool = False

    # Writers

    def update_account(self, account: AccountData) -> None:
        with self._lock:
            self.accounts[account.name] = account
            self.accounts_dirty = True

    def update_position(self, pos: Position) -> None:
        with self._lock:
            key = f"{pos.account}:{pos.instrument}"
            if pos.quantity == 0:
                self.positions.pop(key, None)
            else:
                self.positions[key] = pos
            self.positions_dirty = True

    def update_order(self, order: Order) -> None:
        with self._lock:
            self.orders[order.order_id] = order
            self.orders_dirty = True

    def update_market_data(self, md: MarketData) -> None:
        with self._lock:
            self.market_data[md.instrument] = md
            self.market_data_dirty = True

    def add_candle(self, candle: Candle) -> None:
        with self._lock:
            items = self.candles.setdefault(candle.instrument, [])
            if items and items[-1].timestamp == candle.timestamp:
                items[-1] = candle
            else:
                items.append(candle)
                if len(items) > self._max_candles:
                    items.pop(0)
            self.candles_dirty[candle.instrument] = True

    def add_log(self, message: str) -> None:
        with self._lock:
            self.log_lines.append(message)
            self.logs_dirty = True

    def update_strategy_snapshot(self, snapshot: StrategySnapshot) -> None:
        with self._lock:
            self.strategy_snapshot = snapshot
            self.strategy_mode = snapshot.runtime_state
            if snapshot.timestamp:
                self.strategy_last_event_at = snapshot.timestamp
            if str(snapshot.details.get("reason") or "") == "HEARTBEAT":
                self.strategy_last_heartbeat_at = snapshot.timestamp
            self._recompute_strategy_fault_locked()
            self.strategy_health = RuntimeHealthState.from_snapshot(snapshot, self.strategy_fault)
            self.strategy_dirty = True

    def add_strategy_event(self, event: StrategyEventRecord) -> None:
        with self._lock:
            self.strategy_events.appendleft(event)
            if event.timestamp:
                self.strategy_last_event_at = event.timestamp
            if event.event == "HEARTBEAT":
                self.strategy_last_heartbeat_at = event.timestamp
            if event.event in {"ERROR", "REJECTED", "HEARTBEAT_TIMEOUT"}:
                self.strategy_fault = event.summary or event.error_code or event.event
            elif self.strategy_snapshot is not None:
                self._recompute_strategy_fault_locked()
            if self.strategy_snapshot is not None:
                self.strategy_health = RuntimeHealthState.from_snapshot(
                    self.strategy_snapshot,
                    self.strategy_fault,
                )
            self.strategy_dirty = True

    def update_strategy_clients(self, *, client_count: int, listening: bool, endpoint: str) -> None:
        with self._lock:
            self.strategy_connected_clients = client_count
            self.strategy_api_listening = listening
            self.strategy_api_endpoint = endpoint
            self.strategy_dirty = True

    def clear_strategy_events(self) -> None:
        with self._lock:
            self.strategy_events.clear()
            self.strategy_dirty = True

    # Readers

    def get_accounts(self) -> list[AccountData]:
        with self._lock:
            return list(self.accounts.values())

    def get_positions(self) -> list[Position]:
        with self._lock:
            return list(self.positions.values())

    def get_orders(self) -> list[Order]:
        with self._lock:
            return list(self.orders.values())

    def get_market_data(self) -> list[MarketData]:
        with self._lock:
            return list(self.market_data.values())

    def get_candles(self, instrument: str) -> list[Candle]:
        with self._lock:
            return list(self.candles.get(instrument, []))

    def get_logs(self) -> list[str]:
        with self._lock:
            return list(self.log_lines)

    def get_strategy_snapshot(self) -> StrategySnapshot | None:
        with self._lock:
            return self.strategy_snapshot

    def get_strategy_events(self) -> list[StrategyEventRecord]:
        with self._lock:
            return list(self.strategy_events)

    def clear_trading_data(self) -> None:
        """Wipe stale NT data when the bridge disconnects."""
        with self._lock:
            self.accounts.clear()
            self.positions.clear()
            self.orders.clear()
            self.market_data.clear()
            self.accounts_dirty = True
            self.positions_dirty = True
            self.orders_dirty = True
            self.market_data_dirty = True

    # Dirty-flag helpers

    def check_and_clear(self, flag: str) -> bool:
        """Return True if the flag is set, then clear it atomically."""
        with self._lock:
            val = getattr(self, flag)
            if val:
                setattr(self, flag, False if not isinstance(val, dict) else {})
            return bool(val)

    def candles_check_and_clear(self, instrument: str) -> bool:
        with self._lock:
            val = self.candles_dirty.get(instrument, False)
            if val:
                self.candles_dirty[instrument] = False
            return val

    def _recompute_strategy_fault_locked(self) -> None:
        snapshot = self.strategy_snapshot
        if snapshot is None:
            self.strategy_fault = ""
            return
        if not snapshot.nt_connected:
            self.strategy_fault = "NinjaTrader disconnected"
            return
        if snapshot.heartbeat_faulted:
            self.strategy_fault = "Heartbeat faulted"
            return
        if snapshot.recovery_required:
            self.strategy_fault = snapshot.recovery_reason or "Recovery required"
            return
        if snapshot.protective_orders_faulted:
            self.strategy_fault = "Protective orders missing"
            return
        if snapshot.daily_lockout:
            self.strategy_fault = "Daily lockout active"
            return
        self.strategy_fault = ""
