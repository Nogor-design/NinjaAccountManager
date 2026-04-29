from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.config import AppConfig
from core.data_models import Order, Position
from core.event_bus import EventBus, Events

if TYPE_CHECKING:
    from core.nt_client import NinjaTraderClient

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _timestamp_slug(value: datetime) -> str:
    return value.strftime("%Y%m%d_%H%M%S%f")[:-3]


@dataclass
class BridgeInstruction:
    message_id: str = ""
    timestamp: str = ""
    account: str = ""
    instrument: str = ""
    timeframe: str = ""
    action: str = ""
    side: str = ""
    template_name: str = ""
    confidence: float = 0.0
    entry_mode: str = "market"
    quantity: int = 0
    stop_mode: str = ""
    stop_ticks: int = 0
    stop_price: float | None = None
    target_mode: str = ""
    target_ticks: int | None = None
    partial_target_ticks: int | None = None
    runner_mode: str = ""
    max_hold_bars: int | None = None
    thesis_id: str = ""
    notes: str = ""
    position_id: str = ""
    expected_position_state: str = ""
    signal_expiry_seconds: int | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "BridgeInstruction":
        command = str(payload.get("command") or payload.get("action") or "").strip()
        signal_id = str(
            payload.get("signal_id")
            or payload.get("message_id")
            or payload.get("correlation_id")
            or ""
        ).strip()
        return cls(
            message_id=signal_id,
            timestamp=str(payload.get("timestamp") or "").strip(),
            account=str(payload.get("account") or "").strip(),
            instrument=str(payload.get("instrument") or "").strip(),
            timeframe=str(payload.get("timeframe") or "").strip(),
            action=command,
            side=str(payload.get("side") or "").strip(),
            template_name=str(payload.get("template_name") or "").strip(),
            confidence=float(payload.get("confidence") or 0.0),
            entry_mode=str(payload.get("entry_mode") or "market").strip(),
            quantity=int(payload.get("quantity") or 0),
            stop_mode=str(payload.get("stop_mode") or "").strip(),
            stop_ticks=int(payload.get("stop_ticks") or 0),
            stop_price=_safe_float(payload.get("stop_price")),
            target_mode=str(payload.get("target_mode") or "").strip(),
            target_ticks=_safe_int(payload.get("target_ticks")),
            partial_target_ticks=_safe_int(payload.get("partial_target_ticks")),
            runner_mode=str(payload.get("runner_mode") or "").strip(),
            max_hold_bars=_safe_int(payload.get("max_hold_bars")),
            thesis_id=str(payload.get("thesis_id") or "").strip(),
            notes=str(payload.get("notes") or "").strip(),
            position_id=str(payload.get("position_id") or "").strip(),
            expected_position_state=str(payload.get("expected_position_state") or "").strip(),
            signal_expiry_seconds=_safe_int(payload.get("signal_expiry_seconds")),
        )


@dataclass
class ActiveExecution:
    instruction: BridgeInstruction
    account: str
    side: str
    position_id: str
    entry_signal_name: str
    stop_signal_name: str
    target_signal_name: str
    oco_id: str
    entry_order_id: str = ""
    stop_order_id: str = ""
    target_order_id: str = ""
    entry_filled: bool = False
    protective_submitted: bool = False
    pending_stop_move: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    filled_price: float | None = None
    last_protective_submit_utc: datetime | None = None


@dataclass
class PersistentBridgeState:
    ActiveTemplate: str = ""
    LastInstructionId: str = ""
    SignalIntakeEnabled: bool = True
    HeartbeatFaulted: bool = False
    DailyLockout: bool = False
    CurrentTradingDay: str = ""
    LastBridgeMessageUtc: str = ""
    ShellMode: str = "Idle"
    PositionId: str = ""
    PendingStopPrice: float = 0.0
    PendingTargetTicks: int = 0
    ProcessedIds: list[str] = field(default_factory=list)
    LastHealthSnapshotUtc: str = ""


@dataclass
class PendingCleanupOrder:
    order_id: str
    signal_name: str
    instruction_id: str
    account: str
    instrument: str
    kind: str
    status: str = ""
    first_seen_utc: datetime = field(default_factory=_utc_now)
    last_cancel_attempt_utc: datetime | None = None
    warning_emitted: bool = False
    timeout_emitted: bool = False


class StrategyBridgeService:
    def __init__(self, config: AppConfig, event_bus: EventBus, nt_client: NinjaTraderClient) -> None:
        self._config = config
        self._bus = event_bus
        self._nt = nt_client
        self._lock = threading.RLock()
        self._running = False
        self._thread: threading.Thread | None = None

        self._root = config.bridge_root
        self._inbox = self._root / "inbox"
        self._archive = self._root / "archive"
        self._rejected = self._root / "rejected"
        self._outbox = self._root / "outbox"
        self._state_dir = self._root / "state"
        self._logs_dir = self._root / "logs"
        self._state_file = self._state_dir / "shell_state.json"

        self._positions: dict[tuple[str, str], Position] = {}
        self._orders: dict[str, Order] = {}
        self._processed_ids: list[str] = []
        self._processed_lookup: set[str] = set()
        self._recent_signal_to_instruction: dict[str, str] = {}
        self._emitted_fill_order_ids: set[str] = set()
        self._pending_cleanup_orders: dict[str, PendingCleanupOrder] = {}
        self._active: ActiveExecution | None = None
        self._shell_mode = "Idle"
        self._signal_intake_enabled = True
        self._heartbeat_faulted = False
        self._daily_lockout = False
        self._disabled_by_operator = False
        self._last_instruction_id = ""
        self._active_template = ""
        self._last_bridge_message_utc: datetime | None = None
        self._last_heartbeat_lost_at: datetime | None = None
        self._last_state_flush_monotonic = 0.0
        self._last_snapshot_signature = ""
        self._recovery_required = False
        self._recovery_reason = ""
        self._protective_orders_faulted = False
        self._legacy_file_bridge_enabled = config.legacy_file_bridge_enabled
        self._execution_mode = str(config.execution_mode or "SIM").upper()
        self._strategy_allowed_accounts = {account.strip() for account in config.strategy_allowed_accounts if account.strip()}
        if not self._strategy_allowed_accounts:
            self._strategy_allowed_accounts = {config.bridge_default_account}
        self._live_confirmation_received = self._execution_mode != "LIVE"

    def start(self) -> None:
        if self._legacy_file_bridge_enabled:
            self._ensure_directories()
            self._load_persistent_state()
        self._subscribe()
        if self._execution_mode == "LIVE" and not self._live_confirmation_received:
            self._signal_intake_enabled = False
            self._disabled_by_operator = True
            if self._active is None:
                self._shell_mode = "Disabled"
        self._running = True
        self._write_state()
        self.publish_state_snapshot(reason="runtime_started", force=True)
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="StrategyBridgeThread",
        )
        self._thread.start()
        logger.info("Strategy bridge started (root=%s)", self._root)

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._write_state(force=True)

    def request_state_snapshot(self, reason: str = "operator_request") -> dict[str, Any]:
        snapshot = self.build_state_snapshot(reason=reason)
        self.publish_state_snapshot(reason=reason, force=True)
        return snapshot

    def set_intake_enabled(
        self,
        enabled: bool,
        *,
        confirmed_live: bool = False,
        source: str = "operator",
    ) -> bool:
        signal_id = _operator_signal_id("set_intake")
        if enabled and self._execution_mode == "LIVE" and not (self._live_confirmation_received or confirmed_live):
            self._emit_event(
                "REJECTED",
                signal_id=signal_id,
                detail="live intake confirmation required",
                error_code="live_confirmation_required",
                source=source,
            )
            return False

        with self._lock:
            if enabled:
                if confirmed_live:
                    self._live_confirmation_received = True
                self._signal_intake_enabled = True
                self._disabled_by_operator = False
                if self._active is None and not self._recovery_required and self._shell_mode == "Disabled":
                    self._shell_mode = "Idle"
            else:
                self._signal_intake_enabled = False
                self._disabled_by_operator = True
                if self._active is None and not self._recovery_required:
                    self._shell_mode = "Disabled"

        self._emit_event(
            "ACCEPTED",
            signal_id=signal_id,
            detail=f"signal intake {'enabled' if enabled else 'disabled'}",
            source=source,
        )
        self._write_state(force=True)
        return True

    def submit_operator_command(self, command_payload: dict[str, Any], source: str = "operator") -> BridgeInstruction | None:
        payload = dict(command_payload)
        payload.setdefault("timestamp", _utc_now().isoformat())
        payload.setdefault("signal_id", _operator_signal_id(str(payload.get("command") or payload.get("action") or "operator")))
        payload.setdefault("account", self._active.account if self._active is not None else self._config.bridge_default_account)
        if self._active is not None:
            payload.setdefault("instrument", self._active.instruction.instrument)
        return self.submit_instruction_payload(payload, source=source)

    def clear_recovery_if_flat(self, source: str = "operator") -> bool:
        signal_id = _operator_signal_id("ack_recovery")
        with self._lock:
            has_live_position = any(
                position.account == self._config.bridge_default_account and position.quantity != 0
                for position in self._positions.values()
            )
            if has_live_position:
                self._emit_event(
                    "REJECTED",
                    signal_id=signal_id,
                    detail="cannot clear recovery while position is live",
                    error_code="recovery_not_flat",
                    source=source,
                )
                return False
            self._recovery_required = False
            self._recovery_reason = ""
            if self._active is None and not self._disabled_by_operator:
                self._shell_mode = "Idle"
            elif self._active is None and self._disabled_by_operator:
                self._shell_mode = "Disabled"

        self._emit_event(
            "ACCEPTED",
            signal_id=signal_id,
            detail="recovery cleared",
            source=source,
        )
        self._write_state(force=True)
        return True

    def process_inbox_once(self) -> None:
        if self._legacy_file_bridge_enabled:
            self._ensure_directories()
        self._check_heartbeat_timeout()
        self._detect_missing_protective_orders()
        self._retry_pending_cleanup_orders()
        if self._legacy_file_bridge_enabled:
            for path in sorted(self._inbox.glob("*.json")):
                self._process_instruction_file(path)
        self._write_state()

    def _run_loop(self) -> None:
        while self._running:
            try:
                self.process_inbox_once()
            except Exception:  # noqa: BLE001
                logger.exception("Strategy bridge loop error.")
            time.sleep(self._config.bridge_poll_interval_seconds)

    def submit_instruction_payload(self, payload: dict[str, Any], source: str = "socket") -> BridgeInstruction | None:
        try:
            instruction = BridgeInstruction.from_payload(payload)
        except (ValueError, TypeError):
            self._emit_event(
                "REJECTED",
                signal_id=str(payload.get("signal_id") or payload.get("message_id") or ""),
                detail="payload parse failed",
                error_code="payload_parse_failed",
                source=source,
            )
            return None

        rejection = self._validate_instruction(instruction)
        if rejection is not None:
            self._last_instruction_id = instruction.message_id
            if instruction.template_name:
                self._active_template = instruction.template_name
            self._record_processed_id(instruction.message_id)
            self._emit_event(
                "REJECTED",
                signal_id=instruction.message_id,
                instruction=instruction,
                detail=rejection,
                error_code=_slugify(rejection),
                source=source,
            )
            return None

        self._record_bridge_message(instruction)
        self._record_processed_id(instruction.message_id)
        self._emit_event(
            "ACCEPTED",
            signal_id=instruction.message_id,
            instruction=instruction,
            detail="command accepted",
            source=source,
        )
        self._execute_instruction(instruction)
        return instruction

    def _subscribe(self) -> None:
        self._bus.subscribe(Events.CONNECTED, self._on_connected)
        self._bus.subscribe(Events.POSITION_UPDATE, self._on_position_update)
        self._bus.subscribe(Events.ORDER_UPDATE, self._on_order_update)
        self._bus.subscribe(Events.DISCONNECTED, self._on_disconnect)

    def _nt_connection_count(self) -> int:
        return _safe_connection_count(self._nt)

    def _ensure_directories(self) -> None:
        for directory in (
            self._root,
            self._inbox,
            self._archive,
            self._rejected,
            self._outbox,
            self._state_dir,
            self._logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def _load_persistent_state(self) -> None:
        if not self._state_file.exists():
            return
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Unable to read existing strategy bridge state.")
            return

        self._signal_intake_enabled = bool(raw.get("SignalIntakeEnabled", True))
        self._heartbeat_faulted = bool(raw.get("HeartbeatFaulted", False))
        self._daily_lockout = bool(raw.get("DailyLockout", False))
        self._shell_mode = str(raw.get("ShellMode") or "Idle")
        self._disabled_by_operator = self._shell_mode == "Disabled"
        self._last_instruction_id = str(raw.get("LastInstructionId") or "")
        self._active_template = str(raw.get("ActiveTemplate") or "")
        self._processed_ids = [
            str(value).strip()
            for value in raw.get("ProcessedIds", [])
            if str(value).strip()
        ][-self._config.bridge_processed_ids_retain_count :]
        self._processed_lookup = set(self._processed_ids)
        self._last_bridge_message_utc = _parse_timestamp(str(raw.get("LastBridgeMessageUtc") or ""))

    def _process_instruction_file(self, path: Path) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            instruction = BridgeInstruction.from_payload(payload)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            self._reject_file(path, "payload parse failed", instruction=None)
            return

        rejection = self._validate_instruction(instruction)
        if rejection is not None:
            self._reject_file(path, rejection, instruction=instruction)
            return

        self._record_bridge_message(instruction)
        self._archive_file(path, instruction)
        self._write_outbox_event(
            "ACCEPTED",
            instruction.message_id,
            f"id={instruction.message_id};action={instruction.action};template={instruction.template_name}",
        )
        self._execute_instruction(instruction)

    def _validate_instruction(self, instruction: BridgeInstruction) -> str | None:
        if not instruction.message_id:
            return "missing message_id"
        if instruction.message_id in self._processed_lookup:
            return "duplicate message_id"

        ts = _parse_timestamp(instruction.timestamp)
        if ts is None:
            return "invalid timestamp"

        expiry_seconds = instruction.signal_expiry_seconds or self._config.bridge_stale_signal_seconds
        now = _utc_now()
        if now - ts > timedelta(seconds=expiry_seconds):
            return "stale signal"
        if ts - now > timedelta(seconds=self._config.bridge_future_skew_seconds):
            return "future timestamp"

        action = instruction.action.upper()
        supported = {
            "HEARTBEAT",
            "ENTER_LONG",
            "ENTER_SHORT",
            "EXIT_ALL",
            "SCRATCH",
            "MOVE_STOP",
            "CANCEL_WORKING",
            "FLATTEN_AND_DISABLE",
        }
        if action not in supported:
            return "unsupported action"

        if action in {"ENTER_LONG", "ENTER_SHORT"}:
            account_name = instruction.account or self._config.bridge_default_account
            if not instruction.template_name:
                return "missing template_name"
            if not instruction.instrument:
                return "missing instrument"
            if account_name not in self._strategy_allowed_accounts:
                return "account not allowed for strategy execution"
            if instruction.quantity <= 0:
                return "invalid quantity"
            if instruction.quantity > self._config.bridge_max_position_size:
                return "quantity above max position size"
            if instruction.stop_ticks > self._config.bridge_max_stop_ticks_cap:
                return "stop ticks above configured cap"
            if instruction.entry_mode.lower() not in {"market"}:
                return "unsupported entry_mode"
            if not self._nt.is_connected:
                return "nt_not_connected"
            if self._config.strategy_require_single_nt_connection and self._nt_connection_count() != 1:
                return "expected exactly one ninjatrader connection"
            if not self._signal_intake_enabled:
                return "signal intake disabled"
            if self._heartbeat_faulted:
                return "heartbeat faulted"
            if self._daily_lockout:
                return "daily loss lockout active"
            if self._recovery_required:
                return "recovery required before new entry"
            if self._active is not None or self._has_open_position(instruction.instrument):
                return "one_trade_at_a_time"

        if action == "MOVE_STOP" and (instruction.stop_price is None or instruction.stop_price <= 0):
            return "invalid stop price"

        if self._config.bridge_require_instrument_match and self._active is not None:
            if instruction.instrument and instruction.instrument != self._active.instruction.instrument:
                return "instrument mismatch"

        return None

    def _record_bridge_message(self, instruction: BridgeInstruction) -> None:
        with self._lock:
            self._last_bridge_message_utc = _utc_now()
            self._last_instruction_id = instruction.message_id
            if instruction.template_name:
                self._active_template = instruction.template_name
            if self._heartbeat_faulted:
                logger.info("Strategy bridge heartbeat restored by message %s", instruction.message_id)
            self._heartbeat_faulted = False
            self._last_heartbeat_lost_at = None
            if not self._disabled_by_operator:
                self._signal_intake_enabled = True
                if self._shell_mode == "Disabled":
                    self._shell_mode = "Idle"

    def _archive_file(self, path: Path, instruction: BridgeInstruction) -> None:
        destination = self._archive / path.name
        path.replace(destination)
        self._record_processed_id(instruction.message_id)

    def _reject_file(self, path: Path, reason: str, instruction: BridgeInstruction | None) -> None:
        instruction_id = instruction.message_id if instruction is not None else path.stem
        destination = self._rejected / path.name
        try:
            path.replace(destination)
        except OSError:
            logger.exception("Failed moving rejected instruction %s", path)
        if instruction is not None and instruction.message_id:
            self._last_instruction_id = instruction.message_id
            if instruction.template_name:
                self._active_template = instruction.template_name
            self._record_processed_id(instruction.message_id)
        self._write_outbox_event(
            "REJECTED",
            instruction_id,
            f"id={instruction_id};action={(instruction.action if instruction else '?')};reason={reason}",
        )
        logger.warning("Rejected bridge instruction %s: %s", instruction_id, reason)

    def _execute_instruction(self, instruction: BridgeInstruction) -> None:
        action = instruction.action.upper()

        if action == "HEARTBEAT":
            self._write_outbox_event("HEARTBEAT", instruction.message_id, f"id={instruction.message_id}")
            self._write_state(force=True)
            return

        if action in {"ENTER_LONG", "ENTER_SHORT"}:
            self._handle_entry(instruction)
            return

        if action in {"EXIT_ALL", "SCRATCH"}:
            self._handle_exit(instruction)
            return

        if action == "MOVE_STOP":
            self._handle_move_stop(instruction)
            return

        if action == "CANCEL_WORKING":
            self._handle_cancel_working(instruction)
            return

        if action == "FLATTEN_AND_DISABLE":
            self._signal_intake_enabled = False
            self._disabled_by_operator = True
            self._shell_mode = "Disabled"
            self._handle_exit(instruction, disable_after_flatten=True)
            return

    def _handle_entry(self, instruction: BridgeInstruction) -> None:
        side = "LONG" if instruction.action.upper() == "ENTER_LONG" else "SHORT"
        entry_signal_name = f"TF_ENTER_{instruction.message_id}"
        active = ActiveExecution(
            instruction=instruction,
            account=instruction.account or self._config.bridge_default_account,
            side=side,
            position_id=instruction.position_id or instruction.message_id,
            entry_signal_name=entry_signal_name,
            stop_signal_name=f"TF_STOP_{instruction.message_id}",
            target_signal_name=f"TF_TARGET_{instruction.message_id}",
            oco_id=f"TF_OCO_{instruction.message_id}",
        )

        with self._lock:
            self._active = active
            self._shell_mode = "EntryPending"
            self._protective_orders_faulted = False
            self._recent_signal_to_instruction[active.entry_signal_name] = instruction.message_id
            self._recent_signal_to_instruction[active.stop_signal_name] = instruction.message_id
            self._recent_signal_to_instruction[active.target_signal_name] = instruction.message_id

        order_action = "Buy" if side == "LONG" else "Sell"
        self._nt.submit_order(
            account=active.account,
            instrument=instruction.instrument,
            action=order_action,
            order_type="Market",
            quantity=instruction.quantity,
            price=0.0,
            stop_price=0.0,
            signal_name=active.entry_signal_name,
            oco_id="",
        )
        self._write_outbox_event(
            "ENTRY_SUBMITTED",
            instruction.message_id,
            (
                f"id={instruction.message_id};side={side};qty={instruction.quantity};"
                f"stop_ticks={instruction.stop_ticks};target_ticks={instruction.target_ticks or 0}"
            ),
        )
        logger.info(
            "Entry routed to NinjaTrader: msg=%s instrument=%s side=%s qty=%s template=%s",
            instruction.message_id,
            instruction.instrument,
            side,
            instruction.quantity,
            instruction.template_name,
        )

    def _handle_exit(self, instruction: BridgeInstruction, disable_after_flatten: bool = False) -> None:
        active = self._active
        if active is None:
            if disable_after_flatten:
                self._shell_mode = "Disabled"
                self._signal_intake_enabled = False
                self._disabled_by_operator = True
                self._write_outbox_event("FLATTENED", instruction.message_id, "reason=no_active_execution")
                return
            self._write_outbox_event(
                "REJECTED",
                instruction.message_id,
                f"id={instruction.message_id};action={instruction.action};reason=no active execution",
            )
            return

        position = self._positions.get((active.account, active.instruction.instrument))
        if position is None or position.quantity == 0:
            if disable_after_flatten:
                self._shell_mode = "Disabled"
                self._active = None
                self._write_outbox_event("FLATTENED", instruction.message_id, "reason=already_flat")
            else:
                self._write_outbox_event(
                    "REJECTED",
                    instruction.message_id,
                    f"id={instruction.message_id};action={instruction.action};reason=no active position",
                )
            return

        self._cancel_protective_orders(active)

        qty = abs(position.quantity)
        if instruction.action.upper() == "SCRATCH":
            qty = min(qty, 1)
        exit_action = "Sell" if position.quantity > 0 else "Buy"
        exit_signal_name = f"TF_EXIT_{instruction.message_id}"
        self._recent_signal_to_instruction[exit_signal_name] = active.instruction.message_id
        self._nt.submit_order(
            account=active.account,
            instrument=active.instruction.instrument,
            action=exit_action,
            order_type="Market",
            quantity=qty,
            signal_name=exit_signal_name,
            oco_id="",
        )
        self._write_outbox_event(
            "EXIT_SUBMITTED",
            instruction.message_id,
            f"id={instruction.message_id};qty={qty};disable_after_flatten={disable_after_flatten}",
        )
        if disable_after_flatten:
            self._signal_intake_enabled = False
            self._disabled_by_operator = True

    def _handle_move_stop(self, instruction: BridgeInstruction) -> None:
        active = self._active
        if active is None:
            self._write_outbox_event(
                "REJECTED",
                instruction.message_id,
                f"id={instruction.message_id};action=MOVE_STOP;reason=no active execution",
            )
            return

        position = self._positions.get((active.account, active.instruction.instrument))
        if position is None or position.quantity == 0:
            self._write_outbox_event(
                "REJECTED",
                instruction.message_id,
                f"id={instruction.message_id};action=MOVE_STOP;reason=no active position",
            )
            return

        new_stop = round(instruction.stop_price / self._config.bridge_tick_size) * self._config.bridge_tick_size
        if position.quantity > 0 and new_stop >= position.avg_price:
            self._write_outbox_event(
                "REJECTED",
                instruction.message_id,
                f"id={instruction.message_id};action=MOVE_STOP;reason=unsafe stop level",
            )
            return
        if position.quantity < 0 and new_stop <= position.avg_price:
            self._write_outbox_event(
                "REJECTED",
                instruction.message_id,
                f"id={instruction.message_id};action=MOVE_STOP;reason=unsafe stop level",
            )
            return

        active.pending_stop_move = new_stop
        if active.stop_order_id:
            self._nt.cancel_order(active.stop_order_id)
        else:
            self._submit_stop_order(active, position.avg_price, abs(position.quantity), override_stop=new_stop)
            self._write_outbox_event(
                "STOP_MOVED",
                instruction.message_id,
                f"id={instruction.message_id};stop_price={new_stop}",
            )

    def _handle_cancel_working(self, instruction: BridgeInstruction) -> None:
        active = self._active
        if active is None:
            self._write_outbox_event(
                "REJECTED",
                instruction.message_id,
                f"id={instruction.message_id};action=CANCEL_WORKING;reason=no active execution",
            )
            return

        cancelled = False
        for order_id in (active.entry_order_id, active.stop_order_id, active.target_order_id):
            if order_id:
                self._nt.cancel_order(order_id)
                cancelled = True
        if not cancelled:
            self._write_outbox_event(
                "REJECTED",
                instruction.message_id,
                f"id={instruction.message_id};action=CANCEL_WORKING;reason=no working orders",
            )
            return
        self._write_outbox_event("ACCEPTED", instruction.message_id, f"id={instruction.message_id};action=CANCEL_WORKING")

    def _track_cleanup_order(
        self,
        *,
        order_id: str,
        signal_name: str,
        instruction_id: str,
        account: str,
        instrument: str,
        kind: str,
    ) -> None:
        if not order_id:
            return
        existing = self._pending_cleanup_orders.get(order_id)
        if existing is not None:
            existing.signal_name = signal_name or existing.signal_name
            existing.instruction_id = instruction_id or existing.instruction_id
            existing.account = account or existing.account
            existing.instrument = instrument or existing.instrument
            existing.kind = kind or existing.kind
            return
        self._pending_cleanup_orders[order_id] = PendingCleanupOrder(
            order_id=order_id,
            signal_name=signal_name,
            instruction_id=instruction_id,
            account=account,
            instrument=instrument,
            kind=kind,
        )

    def _track_cleanup_orders_for_active(self, active: ActiveExecution) -> None:
        self._track_cleanup_order(
            order_id=active.stop_order_id,
            signal_name=active.stop_signal_name,
            instruction_id=active.instruction.message_id,
            account=active.account,
            instrument=active.instruction.instrument,
            kind="stop",
        )
        self._track_cleanup_order(
            order_id=active.target_order_id,
            signal_name=active.target_signal_name,
            instruction_id=active.instruction.message_id,
            account=active.account,
            instrument=active.instruction.instrument,
            kind="target",
        )

    def _on_position_update(self, position: Position) -> None:
        key = (position.account, position.instrument)
        with self._lock:
            self._positions[key] = position
            active = self._active

        if position.account == self._config.bridge_default_account:
            logger.info(
                "Strategy position update: account=%s instrument=%s qty=%s avg=%.2f active_signal=%s runtime=%s",
                position.account,
                position.instrument,
                position.quantity,
                position.avg_price,
                active.instruction.message_id if active is not None else "<none>",
                self._shell_mode,
            )

        if active is None:
            if position.account == self._config.bridge_default_account and position.quantity != 0:
                if not self._recovery_required:
                    self._recovery_required = True
                    self._recovery_reason = "live_position_detected_without_runtime_context"
                    self._shell_mode = "RecoveryRequired"
                    self._emit_event(
                        "ERROR",
                        signal_id=self._last_instruction_id,
                        detail=self._recovery_reason,
                        error_code="recovery_required",
                        extra={
                            "instrument": position.instrument,
                            "quantity": position.quantity,
                            "avg_price": position.avg_price,
                        },
                    )
            elif position.account == self._config.bridge_default_account and position.quantity == 0 and self._recovery_required:
                self._recovery_required = False
                self._recovery_reason = ""
                self._shell_mode = "Idle"
                self.publish_state_snapshot(reason="recovery_cleared", force=True)
            return
        if key != (active.account, active.instruction.instrument):
            return

        if position.quantity == 0:
            self._complete_active_execution()
            return

        with self._lock:
            if self._shell_mode != "Disabled":
                self._shell_mode = "InPosition"

        if active.entry_filled and not active.protective_submitted:
            self._attach_protective_orders(active, position)

    def _on_order_update(self, order: Order) -> None:
        with self._lock:
            self._orders[order.order_id] = order
            active = self._active

        signal_name = (order.signal_name or "").strip()
        if signal_name.startswith("TF_"):
            logger.info(
                "Strategy order update: signal=%s order_id=%s status=%s qty=%s filled=%s action=%s type=%s price=%.2f stop=%.2f active_signal=%s runtime=%s",
                signal_name,
                order.order_id,
                order.status,
                order.quantity,
                order.filled_quantity,
                order.action,
                order.order_type,
                order.price,
                order.stop_price,
                active.instruction.message_id if active is not None else "<none>",
                self._shell_mode,
            )
        if active is None:
            self._handle_pending_cleanup_update(order)
            self._handle_late_fill_update(signal_name, order)
            return

        if signal_name == active.entry_signal_name:
            self._handle_entry_order_update(active, order)
            return
        if signal_name == active.stop_signal_name:
            self._handle_stop_order_update(active, order)
            return
        if signal_name == active.target_signal_name:
            self._handle_target_order_update(active, order)
            return
        if signal_name.startswith("TF_EXIT_") and order.status == "Filled":
            instruction_id = self._recent_signal_to_instruction.get(signal_name)
            if instruction_id is None:
                logger.info("Ignoring unmapped exit fill signal=%s order_id=%s", signal_name, order.order_id)
                return
            instruction = active.instruction if active.instruction.message_id == instruction_id else None
            self._emit_exit_filled_event(
                instruction_id,
                instruction,
                signal_name,
                order,
                order.price or 0.0,
                exit_reason="manual",
            )

    def _handle_entry_order_update(self, active: ActiveExecution, order: Order) -> None:
        active.entry_order_id = order.order_id or active.entry_order_id

        if order.status == "Rejected":
            self._emit_event(
                "ERROR",
                signal_id=active.instruction.message_id,
                instruction=active.instruction,
                detail="entry order rejected",
                error_code="order_rejected",
                extra={"signal_name": active.entry_signal_name, "order_id": order.order_id},
            )
            self._clear_active_execution(idle_only=True)
            return

        if order.status == "Cancelled":
            self._write_outbox_event(
                "ORDER_CANCELLED",
                active.instruction.message_id,
                f"id={active.instruction.message_id};signal={active.entry_signal_name};order_id={order.order_id}",
            )
            self._clear_active_execution(idle_only=True)
            return

        if order.status in {"PartiallyFilled", "PartFilled"} and order.filled_quantity > 0:
            self._emit_event(
                "PARTIAL_FILL",
                signal_id=active.instruction.message_id,
                instruction=active.instruction,
                detail="entry order partially filled",
                extra={
                    "signal_name": active.entry_signal_name,
                    "order_id": order.order_id,
                    "filled_quantity": order.filled_quantity,
                },
            )

        if order.status == "Filled":
            active.entry_filled = True
            position = self._positions.get((active.account, active.instruction.instrument))
            active.filled_price = position.avg_price if position is not None else (order.price or None)
            self._emit_filled_event(
                active.instruction.message_id,
                active.entry_signal_name,
                order,
                active.filled_price or 0.0,
            )
            if position is not None and position.quantity != 0 and not active.protective_submitted:
                self._attach_protective_orders(active, position)

    def _handle_stop_order_update(self, active: ActiveExecution, order: Order) -> None:
        active.stop_order_id = order.order_id or active.stop_order_id
        if order.status == "Working":
            self._protective_orders_faulted = False
            self._emit_event(
                "STOP_WORKING",
                signal_id=active.instruction.message_id,
                instruction=active.instruction,
                detail="stop order working",
                extra={"signal_name": active.stop_signal_name, "stop_price": active.stop_price or 0.0},
            )
        if order.status == "Cancelled" and active.pending_stop_move is not None:
            position = self._positions.get((active.account, active.instruction.instrument))
            if position is not None and position.quantity != 0:
                moved_to = active.pending_stop_move
                self._submit_stop_order(
                    active,
                    position.avg_price,
                    abs(position.quantity),
                    override_stop=moved_to,
                )
                active.pending_stop_move = None
                self._write_outbox_event(
                    "STOP_MOVED",
                    active.instruction.message_id,
                    f"id={active.instruction.message_id};stop_price={moved_to}",
                )
        if order.status == "Filled":
            self._emit_exit_filled_event(
                active.instruction.message_id,
                active.instruction,
                active.stop_signal_name,
                order,
                order.stop_price or order.price or 0.0,
                exit_reason="stop",
            )
        if order.status in {"Cancelled", "Rejected", "Filled"}:
            self._pending_cleanup_orders.pop(order.order_id, None)

    def _handle_target_order_update(self, active: ActiveExecution, order: Order) -> None:
        active.target_order_id = order.order_id or active.target_order_id
        if order.status == "Working":
            self._protective_orders_faulted = False
            self._emit_event(
                "TARGET_WORKING",
                signal_id=active.instruction.message_id,
                instruction=active.instruction,
                detail="target order working",
                extra={"signal_name": active.target_signal_name, "target_price": active.target_price or 0.0},
            )
        if order.status == "Filled":
            self._emit_exit_filled_event(
                active.instruction.message_id,
                active.instruction,
                active.target_signal_name,
                order,
                order.price or 0.0,
                exit_reason="target",
            )
        if order.status in {"Cancelled", "Rejected", "Filled"}:
            self._pending_cleanup_orders.pop(order.order_id, None)

    def _attach_protective_orders(self, active: ActiveExecution, position: Position) -> None:
        qty = abs(position.quantity)
        if qty == 0:
            return

        active.last_protective_submit_utc = _utc_now()
        self._submit_stop_order(active, position.avg_price, qty)
        self._submit_target_order(active, position.avg_price, qty)
        active.protective_submitted = True
        self._emit_event(
            "STOP_ATTACHED",
            signal_id=active.instruction.message_id,
            instruction=active.instruction,
            detail="protective stop attached",
            extra={"fill_price": position.avg_price, "stop_price": active.stop_price or 0.0},
        )
        if active.target_price is not None:
            self._emit_event(
                "TARGET_ATTACHED",
                signal_id=active.instruction.message_id,
                instruction=active.instruction,
                detail="protective target attached",
                extra={"fill_price": position.avg_price, "target_price": active.target_price or 0.0},
            )
        

    def _submit_stop_order(
        self,
        active: ActiveExecution,
        fill_price: float,
        quantity: int,
        override_stop: float | None = None,
    ) -> None:
        stop_price = override_stop if override_stop is not None else self._resolve_stop_price(active, fill_price)
        stop_action = "Sell" if active.side == "LONG" else "Buy"
        active.stop_price = stop_price
        active.pending_stop_move = None
        self._nt.submit_order(
            account=active.account,
            instrument=active.instruction.instrument,
            action=stop_action,
            order_type="Stop",
            quantity=quantity,
            price=0.0,
            stop_price=stop_price,
            signal_name=active.stop_signal_name,
            oco_id=active.oco_id,
        )

    def _submit_target_order(self, active: ActiveExecution, fill_price: float, quantity: int) -> None:
        target_ticks = active.instruction.target_ticks
        if target_ticks is None or target_ticks <= 0:
            active.target_price = None
            return
        target_price = self._resolve_target_price(active, fill_price)
        target_action = "Sell" if active.side == "LONG" else "Buy"
        active.target_price = target_price
        self._nt.submit_order(
            account=active.account,
            instrument=active.instruction.instrument,
            action=target_action,
            order_type="Limit",
            quantity=quantity,
            price=target_price,
            stop_price=0.0,
            signal_name=active.target_signal_name,
            oco_id=active.oco_id,
        )

    def _resolve_stop_price(self, active: ActiveExecution, fill_price: float) -> float:
        if active.instruction.stop_price is not None and active.instruction.stop_price > 0:
            return _round_to_tick(active.instruction.stop_price, self._config.bridge_tick_size)
        stop_ticks = max(1, active.instruction.stop_ticks or 12)
        delta = stop_ticks * self._config.bridge_tick_size
        raw = fill_price - delta if active.side == "LONG" else fill_price + delta
        return _round_to_tick(raw, self._config.bridge_tick_size)

    def _resolve_target_price(self, active: ActiveExecution, fill_price: float) -> float:
        target_ticks = max(1, active.instruction.target_ticks or active.instruction.stop_ticks or 12)
        delta = target_ticks * self._config.bridge_tick_size
        raw = fill_price + delta if active.side == "LONG" else fill_price - delta
        return _round_to_tick(raw, self._config.bridge_tick_size)

    def _cancel_protective_orders(self, active: ActiveExecution) -> None:
        for order_id in (active.stop_order_id, active.target_order_id):
            if order_id:
                self._nt.cancel_order(order_id)
        self._track_cleanup_orders_for_active(active)

    def _handle_pending_cleanup_update(self, order: Order) -> None:
        if not order.order_id:
            return
        pending = self._pending_cleanup_orders.get(order.order_id)
        if pending is None:
            return
        pending.status = order.status
        if order.status in {"Cancelled", "Rejected", "Filled"}:
            self._pending_cleanup_orders.pop(order.order_id, None)
            self.publish_state_snapshot(reason="cleanup_resolved", force=True)
            return
        if order.status in {"Working", "Accepted", "Submitted", "Initialized", "CancelPending", "CancelSubmitted"}:
            self.publish_state_snapshot(reason="cleanup_pending", force=True)

    def _retry_pending_cleanup_orders(self) -> None:
        if not self._pending_cleanup_orders:
            return
        now = _utc_now()
        for order_id, pending in list(self._pending_cleanup_orders.items()):
            position = self._positions.get((pending.account, pending.instrument))
            if position is not None and position.quantity != 0:
                continue
            order = self._orders.get(order_id)
            status = (order.status if order is not None else pending.status) or ""
            if status in {"Cancelled", "Rejected", "Filled"}:
                self._pending_cleanup_orders.pop(order_id, None)
                continue
            if not pending.warning_emitted:
                self._emit_event(
                    "ERROR",
                    signal_id=pending.instruction_id,
                    detail=f"protective {pending.kind} still active after flat; retrying cancel",
                    error_code="protective_cleanup_pending",
                    extra={
                        "order_id": order_id,
                        "signal_name": pending.signal_name,
                        "kind": pending.kind,
                        "status": status or "unknown",
                    },
                )
                pending.warning_emitted = True
            should_retry = (
                pending.last_cancel_attempt_utc is None
                or (now - pending.last_cancel_attempt_utc).total_seconds() >= self._config.strategy_cleanup_retry_seconds
            )
            if should_retry:
                self._nt.cancel_order(order_id)
                pending.last_cancel_attempt_utc = now
                logger.warning(
                    "Retrying cleanup cancel for lingering strategy %s order %s (signal=%s status=%s)",
                    pending.kind,
                    order_id,
                    pending.signal_name,
                    status or "unknown",
                )
            if (
                not pending.timeout_emitted
                and (now - pending.first_seen_utc).total_seconds() >= self._config.strategy_cleanup_timeout_seconds
            ):
                self._emit_event(
                    "ERROR",
                    signal_id=pending.instruction_id,
                    detail=f"protective {pending.kind} cancel still pending after timeout",
                    error_code="protective_cleanup_timeout",
                    extra={
                        "order_id": order_id,
                        "signal_name": pending.signal_name,
                        "kind": pending.kind,
                        "status": status or "unknown",
                    },
                )
                pending.timeout_emitted = True

    def _handle_late_fill_update(self, signal_name: str, order: Order) -> None:
        instruction_id = self._recent_signal_to_instruction.get(signal_name)
        if instruction_id is None or order.status != "Filled":
            return
        price = order.stop_price or order.price or 0.0
        if signal_name.startswith("TF_ENTER_"):
            self._emit_filled_event(instruction_id, signal_name, order, price)
        else:
            self._emit_exit_filled_event(
                instruction_id,
                None,
                signal_name,
                order,
                price,
                exit_reason="late_fill",
            )

    def _on_connected(self, data: dict[str, Any]) -> None:
        if self._config.strategy_require_single_nt_connection and self._nt_connection_count() != 1:
            self._emit_event(
                "ERROR",
                signal_id=self._last_instruction_id,
                detail="multiple ninjatrader connections detected",
                error_code="multiple_nt_connections",
                extra={"nt_connection_count": self._nt_connection_count()},
            )
        self._emit_event(
            "STATE_SNAPSHOT",
            signal_id=self._last_instruction_id,
            detail="nt_connected",
            extra={"client_id": data.get("client_id", "")},
        )
        self.publish_state_snapshot(reason="nt_connected", force=True)

    def _on_disconnect(self, data: dict[str, Any]) -> None:
        logger.warning("Strategy bridge saw NinjaTrader disconnect: %s", data.get("client_id", ""))
        self._emit_event(
            "ERROR",
            signal_id=self._last_instruction_id,
            detail="ninjatrader disconnected",
            error_code="nt_disconnected",
            extra={"client_id": data.get("client_id", "")},
        )
        self.publish_state_snapshot(reason="nt_disconnected", force=True)

    def _has_open_position(self, instrument: str) -> bool:
        for (account_name, position_instrument), position in self._positions.items():
            if account_name != self._config.bridge_default_account:
                continue
            if instrument and instrument != position_instrument:
                continue
            if position.quantity != 0:
                return True
        return False

    def _complete_active_execution(self) -> None:
        active = self._active
        if active is None:
            return
        self._track_cleanup_orders_for_active(active)
        self._protective_orders_faulted = False
        if self._disabled_by_operator:
            self._shell_mode = "Disabled"
            self._write_outbox_event("FLATTENED", active.instruction.message_id, "reason=flat_position_detected")
        else:
            self._shell_mode = "Idle"
        self._active = None
        if not any(pos.quantity != 0 for pos in self._positions.values()):
            self._recovery_required = False
            self._recovery_reason = ""
        self._write_state(force=True)

    def _clear_active_execution(self, idle_only: bool = False) -> None:
        self._active = None
        self._protective_orders_faulted = False
        if idle_only and self._shell_mode != "Disabled":
            self._shell_mode = "Idle"
        self._write_state(force=True)

    def _check_heartbeat_timeout(self) -> None:
        if self._last_bridge_message_utc is None:
            return
        if self._heartbeat_faulted:
            return
        elapsed = (_utc_now() - self._last_bridge_message_utc).total_seconds()
        if elapsed <= self._config.bridge_heartbeat_timeout_seconds:
            return
        self._heartbeat_faulted = True
        self._signal_intake_enabled = False
        self._last_heartbeat_lost_at = _utc_now()
        instruction_id = self._active.instruction.message_id if self._active is not None else "heartbeat"
        self._emit_event(
            "HEARTBEAT_TIMEOUT",
            signal_id=instruction_id,
            detail="heartbeat timeout",
            error_code="heartbeat_timeout",
            extra={
                "timeout_seconds": self._config.bridge_heartbeat_timeout_seconds,
                "flatten": False,
            },
        )
        logger.warning("Strategy bridge heartbeat lost after %.1fs", elapsed)

    def _detect_missing_protective_orders(self) -> None:
        active = self._active
        if active is None:
            return
        position = self._positions.get((active.account, active.instruction.instrument))
        if position is None or position.quantity == 0 or not active.entry_filled:
            return
        target_required = active.target_price is not None and (active.instruction.target_ticks or 0) > 0
        stop_ready = bool(active.stop_order_id)
        target_ready = (not target_required) or bool(active.target_order_id)
        if stop_ready and target_ready:
            return
        if not active.protective_submitted:
            return
        if active.last_protective_submit_utc is not None:
            elapsed = (_utc_now() - active.last_protective_submit_utc).total_seconds()
            if elapsed < 2.0:
                return
        self._protective_orders_faulted = True
        self._emit_event(
            "ERROR",
            signal_id=active.instruction.message_id,
            instruction=active.instruction,
            detail="protective orders missing after entry fill",
            error_code="protective_orders_missing",
            extra={
                "stop_order_id": active.stop_order_id,
                "target_order_id": active.target_order_id,
                "target_required": target_required,
            },
        )
        self._attach_protective_orders(active, position)

    def _record_processed_id(self, message_id: str) -> None:
        self._processed_ids.append(message_id)
        retain = self._config.bridge_processed_ids_retain_count
        if len(self._processed_ids) > retain:
            self._processed_ids = self._processed_ids[-retain:]
        self._processed_lookup = set(self._processed_ids)

    def build_state_snapshot(self, reason: str = "") -> dict[str, Any]:
        active = self._active
        nt_connection_count = self._nt_connection_count()
        account = ""
        instrument = ""
        position_qty = 0
        avg_price = 0.0
        position_side = "Flat"
        if active is not None:
            account = active.account
            instrument = active.instruction.instrument
            live_position = self._positions.get((active.account, active.instruction.instrument))
            if live_position is not None:
                position_qty = live_position.quantity
                avg_price = live_position.avg_price
                if live_position.quantity > 0:
                    position_side = "Long"
                elif live_position.quantity < 0:
                    position_side = "Short"

        snapshot = {
            "message_type": "EVENT",
            "event": "STATE_SNAPSHOT",
            "signal_id": active.instruction.message_id if active is not None else self._last_instruction_id,
            "correlation_id": active.instruction.message_id if active is not None else self._last_instruction_id,
            "timestamp": _utc_now().isoformat(),
            "account": account or self._config.bridge_default_account,
            "instrument": instrument,
            "runtime_state": self._shell_mode,
            "position_id": active.position_id if active is not None else "",
            "position_side": position_side,
            "position_quantity": position_qty,
            "average_price": avg_price,
            "intake_enabled": self._signal_intake_enabled,
            "heartbeat_faulted": self._heartbeat_faulted,
            "daily_lockout": self._daily_lockout,
            "nt_connected": self._nt.is_connected,
            "nt_connection_count": nt_connection_count,
            "recovery_required": self._recovery_required,
            "recovery_reason": self._recovery_reason,
            "execution_mode": self._execution_mode,
            "live_confirmation_required": self._execution_mode == "LIVE" and not self._live_confirmation_received,
            "live_confirmation_received": self._live_confirmation_received,
            "protective_orders_faulted": self._protective_orders_faulted,
            "details": {
                "reason": reason,
                "active_template": active.instruction.template_name if active is not None else self._active_template,
                "pending_stop_price": active.stop_price if active is not None else 0.0,
                "pending_target_ticks": active.instruction.target_ticks if active is not None else 0,
                "allowed_accounts": sorted(self._strategy_allowed_accounts),
                "cleanup_pending_count": len(self._pending_cleanup_orders),
            },
        }
        return snapshot

    def publish_state_snapshot(self, reason: str = "", force: bool = False) -> None:
        snapshot = self.build_state_snapshot(reason=reason)
        signature = json.dumps(snapshot, sort_keys=True, default=str)
        if not force and signature == self._last_snapshot_signature:
            return
        self._last_snapshot_signature = signature
        self._bus.publish(Events.STRATEGY_EVENT, snapshot)

    def _emit_event(
        self,
        event_type: str,
        signal_id: str,
        *,
        instruction: BridgeInstruction | None = None,
        detail: str = "",
        source: str = "runtime",
        error_code: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        account = ""
        instrument = ""
        if instruction is not None:
            account = instruction.account or self._config.bridge_default_account
            instrument = instruction.instrument
        elif self._active is not None:
            account = self._active.account
            instrument = self._active.instruction.instrument

        event = {
            "message_type": "EVENT",
            "event": event_type,
            "signal_id": signal_id,
            "correlation_id": signal_id,
            "timestamp": _utc_now().isoformat(),
            "account": account,
            "instrument": instrument,
            "runtime_state": self._shell_mode,
            "position_id": self._active.position_id if self._active is not None else "",
            "source": source,
            "error_code": error_code,
            "details": {"message": detail},
        }
        if extra:
            event["details"].update(extra)
        self._bus.publish(Events.STRATEGY_EVENT, event)
        if self._legacy_file_bridge_enabled:
            legacy_status = _legacy_event_name(event_type)
            self._write_legacy_outbox_event(
                legacy_status,
                signal_id,
                _legacy_detail_text(detail, extra),
            )
        self.publish_state_snapshot(reason=event_type)

    def _write_outbox_event(self, status: str, instruction_id: str, detail: str) -> None:
        # Compatibility wrapper kept for the legacy file transport.
        self._emit_event(
            _direct_event_name(status),
            signal_id=instruction_id,
            detail=detail,
            extra=_parse_detail_fields(detail),
        )

    def _write_legacy_outbox_event(self, status: str, instruction_id: str, detail: str) -> None:
        now = _utc_now()
        file_name = f"{_timestamp_slug(now)}_{status}_{instruction_id}.evt.json"
        position = "Flat"
        quantity = 0
        if self._active is not None:
            live_position = self._positions.get((self._active.account, self._active.instruction.instrument))
            if live_position is not None:
                if live_position.quantity > 0:
                    position = "Long"
                elif live_position.quantity < 0:
                    position = "Short"
                quantity = abs(live_position.quantity)
        payload = {
            "timestamp_utc": now.isoformat(),
            "status": status,
            "instruction_id": instruction_id,
            "shell_mode": self._shell_mode,
            "position": position,
            "quantity": quantity,
            "detail": detail,
        }
        (self._outbox / file_name).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _emit_filled_event(self, instruction_id: str, signal_name: str, order: Order, price: float) -> None:
        if order.order_id and order.order_id in self._emitted_fill_order_ids:
            return
        if order.order_id:
            self._emitted_fill_order_ids.add(order.order_id)
        instruction = self._active.instruction if self._active is not None else None
        self._emit_event(
            "FILLED",
            signal_id=instruction_id,
            instruction=instruction,
            detail="entry filled",
            extra={"signal_name": signal_name, "order_id": order.order_id, "price": price},
        )

    def _emit_exit_filled_event(
        self,
        instruction_id: str,
        instruction: BridgeInstruction | None,
        signal_name: str,
        order: Order,
        price: float,
        *,
        exit_reason: str,
    ) -> None:
        if order.order_id and order.order_id in self._emitted_fill_order_ids:
            return
        if order.order_id:
            self._emitted_fill_order_ids.add(order.order_id)
        self._emit_event(
            "EXIT_FILLED",
            signal_id=instruction_id,
            instruction=instruction,
            detail="exit filled",
            extra={
                "signal_name": signal_name,
                "order_id": order.order_id,
                "price": price,
                "exit_reason": exit_reason,
            },
        )

    def _write_state(self, force: bool = False) -> None:
        if not self._legacy_file_bridge_enabled:
            self.publish_state_snapshot(reason="state_refresh", force=force)
            return
        now_monotonic = time.monotonic()
        if not force and (now_monotonic - self._last_state_flush_monotonic) < self._config.bridge_state_flush_seconds:
            return

        active = self._active
        state = PersistentBridgeState(
            ActiveTemplate=active.instruction.template_name if active is not None else self._active_template,
            LastInstructionId=active.instruction.message_id if active is not None else self._last_instruction_id,
            SignalIntakeEnabled=self._signal_intake_enabled,
            HeartbeatFaulted=self._heartbeat_faulted,
            DailyLockout=self._daily_lockout,
            CurrentTradingDay=_utc_now().strftime("%Y-%m-%d"),
            LastBridgeMessageUtc=self._last_bridge_message_utc.isoformat() if self._last_bridge_message_utc else "",
            ShellMode=self._shell_mode,
            PositionId=active.position_id if active is not None else "",
            PendingStopPrice=float(active.stop_price or 0.0) if active is not None else 0.0,
            PendingTargetTicks=int(active.instruction.target_ticks or 0) if active is not None else 0,
            ProcessedIds=list(self._processed_ids),
            LastHealthSnapshotUtc=_utc_now().isoformat(),
        )
        self._state_file.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
        self._last_state_flush_monotonic = now_monotonic
        self.publish_state_snapshot(reason="state_refresh", force=force)


def _safe_int(value: object) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: object) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    return round(round(price / tick_size) * tick_size, 10)


def _slugify(text: str) -> str:
    return "_".join(part for part in str(text).strip().lower().replace("-", " ").split() if part)


def _operator_signal_id(action: str) -> str:
    return f"operator-{_slugify(action)}-{_timestamp_slug(_utc_now())}"


def _safe_connection_count(client: Any) -> int:
    try:
        return int(getattr(client, "connection_count", 1))
    except (TypeError, ValueError):
        return 1


def _parse_detail_fields(detail: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for part in str(detail or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def _legacy_detail_text(detail: str, extra: dict[str, Any] | None) -> str:
    if not extra:
        return detail
    merged = {"message": detail}
    merged.update(extra)
    return ";".join(f"{key}={value}" for key, value in merged.items() if value not in {None, ""})


def _direct_event_name(status: str) -> str:
    mapping = {
        "ORDER_REJECTED": "ERROR",
        "ORDER_CANCELLED": "REJECTED",
        "HEARTBEAT_LOST": "HEARTBEAT_TIMEOUT",
    }
    return mapping.get(status, status)


def _legacy_event_name(event_type: str) -> str:
    mapping = {
        "HEARTBEAT_TIMEOUT": "HEARTBEAT_LOST",
        "ERROR": "ERROR",
        "TARGET_ATTACHED": "TARGET_ATTACHED",
        "TARGET_WORKING": "TARGET_WORKING",
        "EXIT_FILLED": "EXIT_FILLED",
        "PARTIAL_FILL": "PARTIAL_FILL",
        "STATE_SNAPSHOT": "STATE_SNAPSHOT",
    }
    return mapping.get(event_type, event_type)
