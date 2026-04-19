"""Immutable data-model dataclasses for all NinjaTrader entities."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class AccountData:
    name: str = ""
    balance: float = 0.0
    cash_value: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    initial_margin: float = 0.0
    maintenance_margin: float = 0.0
    buying_power: float = 0.0
    excess: float = 0.0


@dataclass
class Position:
    account: str = ""
    instrument: str = ""
    quantity: int = 0          # negative = short
    avg_price: float = 0.0
    unrealized_pnl: float = 0.0
    market_value: float = 0.0


@dataclass
class Order:
    order_id: str = ""
    account: str = ""
    instrument: str = ""
    action: str = ""           # "Buy" | "Sell"
    order_type: str = ""       # "Market" | "Limit" | "Stop" | "StopLimit"
    quantity: int = 0
    filled_quantity: int = 0
    price: float = 0.0
    stop_price: float = 0.0
    status: str = ""           # "Working" | "Filled" | "Cancelled" | "Rejected"
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


@dataclass
class MarketData:
    instrument: str = ""
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    volume: int = 0


@dataclass
class Candle:
    instrument: str = ""
    timestamp: float = 0.0    # Unix epoch (float seconds)
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
