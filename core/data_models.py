"""Immutable data-model dataclasses for all NinjaTrader entities."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


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
    signal_name: str = ""
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


@dataclass
class StrategySnapshot:
    signal_id: str = ""
    correlation_id: str = ""
    timestamp: str = ""
    account: str = ""
    instrument: str = ""
    runtime_state: str = "Idle"
    position_id: str = ""
    position_side: str = "Flat"
    position_quantity: int = 0
    average_price: float = 0.0
    intake_enabled: bool = False
    heartbeat_faulted: bool = False
    daily_lockout: bool = False
    nt_connected: bool = False
    nt_connection_count: int = 0
    recovery_required: bool = False
    recovery_reason: str = ""
    execution_mode: str = "SIM"
    live_confirmation_required: bool = False
    live_confirmation_received: bool = True
    protective_orders_faulted: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_event(cls, payload: dict[str, Any]) -> "StrategySnapshot":
        details = payload.get("details") or {}
        return cls(
            signal_id=str(payload.get("signal_id") or ""),
            correlation_id=str(payload.get("correlation_id") or ""),
            timestamp=str(payload.get("timestamp") or ""),
            account=str(payload.get("account") or ""),
            instrument=str(payload.get("instrument") or ""),
            runtime_state=str(payload.get("runtime_state") or "Idle"),
            position_id=str(payload.get("position_id") or ""),
            position_side=str(payload.get("position_side") or "Flat"),
            position_quantity=int(payload.get("position_quantity") or 0),
            average_price=float(payload.get("average_price") or 0.0),
            intake_enabled=bool(payload.get("intake_enabled", False)),
            heartbeat_faulted=bool(payload.get("heartbeat_faulted", False)),
            daily_lockout=bool(payload.get("daily_lockout", False)),
            nt_connected=bool(payload.get("nt_connected", False)),
            nt_connection_count=int(payload.get("nt_connection_count") or 0),
            recovery_required=bool(payload.get("recovery_required", False)),
            recovery_reason=str(payload.get("recovery_reason") or ""),
            execution_mode=str(payload.get("execution_mode") or "SIM"),
            live_confirmation_required=bool(payload.get("live_confirmation_required", False)),
            live_confirmation_received=bool(payload.get("live_confirmation_received", True)),
            protective_orders_faulted=bool(payload.get("protective_orders_faulted", False)),
            details=dict(details) if isinstance(details, dict) else {},
        )


@dataclass
class StrategyEventRecord:
    event: str = ""
    signal_id: str = ""
    correlation_id: str = ""
    timestamp: str = ""
    account: str = ""
    instrument: str = ""
    runtime_state: str = ""
    position_id: str = ""
    source: str = ""
    error_code: str = ""
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_event(cls, payload: dict[str, Any]) -> "StrategyEventRecord":
        details = payload.get("details") or {}
        summary = ""
        if isinstance(details, dict):
            message = str(details.get("message") or details.get("reason") or "")
            exit_reason = str(details.get("exit_reason") or "")
            price = details.get("price")
            if exit_reason and message:
                summary = f"{message} ({exit_reason})"
            elif message:
                summary = message
            else:
                summary = exit_reason
            if price not in {None, ""} and payload.get("event") in {"FILLED", "EXIT_FILLED"}:
                try:
                    summary = f"{summary} @ {float(price):.2f}".strip()
                except (TypeError, ValueError):
                    pass
        return cls(
            event=str(payload.get("event") or ""),
            signal_id=str(payload.get("signal_id") or ""),
            correlation_id=str(payload.get("correlation_id") or ""),
            timestamp=str(payload.get("timestamp") or ""),
            account=str(payload.get("account") or ""),
            instrument=str(payload.get("instrument") or ""),
            runtime_state=str(payload.get("runtime_state") or ""),
            position_id=str(payload.get("position_id") or ""),
            source=str(payload.get("source") or ""),
            error_code=str(payload.get("error_code") or ""),
            summary=summary,
            details=dict(details) if isinstance(details, dict) else {},
        )


@dataclass
class StrategyControlCommand:
    command: str = ""
    signal_id: str = ""
    timestamp: str = ""
    account: str = ""
    instrument: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeHealthState:
    runtime_state: str = "Idle"
    execution_mode: str = "SIM"
    nt_connected: bool = False
    intake_enabled: bool = False
    heartbeat_faulted: bool = False
    daily_lockout: bool = False
    recovery_required: bool = False
    protective_orders_faulted: bool = False
    strategy_fault: str = ""
    live_confirmation_required: bool = False
    live_confirmation_received: bool = True

    @classmethod
    def from_snapshot(cls, snapshot: StrategySnapshot, strategy_fault: str = "") -> "RuntimeHealthState":
        return cls(
            runtime_state=snapshot.runtime_state,
            execution_mode=snapshot.execution_mode,
            nt_connected=snapshot.nt_connected,
            intake_enabled=snapshot.intake_enabled,
            heartbeat_faulted=snapshot.heartbeat_faulted,
            daily_lockout=snapshot.daily_lockout,
            recovery_required=snapshot.recovery_required,
            protective_orders_faulted=snapshot.protective_orders_faulted,
            strategy_fault=strategy_fault,
            live_confirmation_required=snapshot.live_confirmation_required,
            live_confirmation_received=snapshot.live_confirmation_received,
        )
