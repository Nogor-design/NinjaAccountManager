"""
Thread-safe application state store.

Background threads (WebSocket callbacks) write here; the GUI main-thread reads
here every render frame.  All public methods acquire an RLock so callers never
need to synchronise themselves.
"""
from __future__ import annotations
import threading
from collections import deque

from core.data_models import AccountData, Position, Order, MarketData, Candle


class AppState:
    def __init__(self, max_log_lines: int = 500, max_candles: int = 200) -> None:
        self._lock = threading.RLock()
        self._max_candles = max_candles
        self._max_log_lines = max_log_lines

        # Trading data
        self.accounts: dict[str, AccountData] = {}
        self.positions: dict[str, Position] = {}   # key: "account:instrument"
        self.orders: dict[str, Order] = {}          # key: order_id
        self.market_data: dict[str, MarketData] = {}
        self.candles: dict[str, list[Candle]] = {}  # key: instrument

        # Connection state
        self.is_server_running: bool = False
        self.nt_connected: bool = False

        # Log ring buffer
        self.log_lines: deque[str] = deque(maxlen=max_log_lines)

        # Dirty flags – set by writers, cleared by GUI after each refresh
        self.accounts_dirty: bool = False
        self.positions_dirty: bool = False
        self.orders_dirty: bool = False
        self.market_data_dirty: bool = False
        self.logs_dirty: bool = False
        self.candles_dirty: dict[str, bool] = {}

    # ── Writers (called from any thread) ──────────────────────────────────────

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
            lst = self.candles.setdefault(candle.instrument, [])
            if lst and lst[-1].timestamp == candle.timestamp:
                lst[-1] = candle   # update in-progress bar
            else:
                lst.append(candle)
                if len(lst) > self._max_candles:
                    lst.pop(0)
            self.candles_dirty[candle.instrument] = True

    def add_log(self, message: str) -> None:
        with self._lock:
            self.log_lines.append(message)
            self.logs_dirty = True

    # ── Readers (called from main/GUI thread) ─────────────────────────────────

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

    def clear_trading_data(self) -> None:
        """Wipe accounts/positions/orders/market-data on NT disconnect so stale
        entries from a previous connection are never shown in the panels."""
        with self._lock:
            self.accounts.clear()
            self.positions.clear()
            self.orders.clear()
            self.market_data.clear()
            self.accounts_dirty    = True
            self.positions_dirty   = True
            self.orders_dirty      = True
            self.market_data_dirty = True

    # ── Dirty-flag helpers ────────────────────────────────────────────────────

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
