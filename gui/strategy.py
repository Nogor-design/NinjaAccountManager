"""Strategy operations console tab."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
import time

import dearpygui.dearpygui as dpg

from core.config import AppConfig
from core.data_models import Order
from core.state import AppState
from core.strategy_bridge import StrategyBridgeService
from gui.dashboard import GREEN, MUTED, RED, WHITE, YELLOW
from tools.strategy_live_smoke import SmokeSettings, _report_path, _write_report, run_smoke

_EVENT_COLORS = {
    "ERROR": RED,
    "REJECTED": RED,
    "HEARTBEAT_TIMEOUT": RED,
    "FILLED": GREEN,
    "STOP_WORKING": GREEN,
    "TARGET_WORKING": YELLOW,
    "ENTRY_SUBMITTED": WHITE,
    "ACCEPTED": WHITE,
}

_ORDER_COLORS = {
    "Working": YELLOW,
    "Accepted": WHITE,
    "Submitted": WHITE,
    "Initialized": WHITE,
    "CancelPending": YELLOW,
    "CancelSubmitted": YELLOW,
    "PartiallyFilled": GREEN,
    "PartFilled": GREEN,
    "Filled": GREEN,
    "Cancelled": MUTED,
    "Rejected": RED,
}


class StrategyPanel:
    def __init__(
        self,
        tab_bar: int | str,
        state: AppState,
        runtime: StrategyBridgeService,
        config: AppConfig,
    ) -> None:
        self._state = state
        self._runtime = runtime
        self._config = config
        self._status_color = WHITE
        self._next_heartbeat_monotonic = 0.0
        self._smoke_lock = threading.Lock()
        self._smoke_running = False
        self._smoke_status = "No smoke suite run yet."
        self._smoke_report_path = ""
        self._smoke_report_summary = ""
        self._last_strategy_orders_signature = ""
        self._build(tab_bar)

    def _build(self, tab_bar: int | str) -> None:
        with dpg.tab(label="  Strategy  ", parent=tab_bar, tag="tab_strategy"):
            dpg.add_spacer(height=6)
            with dpg.group(horizontal=True):
                with dpg.child_window(width=470, height=430, border=True):
                    dpg.add_text("STRATEGY RUNTIME", color=MUTED)
                    dpg.add_spacer(height=4)
                    dpg.add_text("", tag="strategy_live_badge", color=YELLOW)
                    dpg.add_text("", tag="strategy_warning_text", wrap=430, color=RED)
                    dpg.add_spacer(height=6)
                    with dpg.table(
                        header_row=False,
                        borders_innerV=False,
                        borders_innerH=False,
                        borders_outerV=False,
                        borders_outerH=False,
                        policy=dpg.mvTable_SizingFixedFit,
                    ):
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=150)
                        dpg.add_table_column(width_stretch=True)
                        self._status_row("API Endpoint", "strategy_api_endpoint")
                        self._status_row("API Clients", "strategy_api_clients")
                        self._status_row("NT Connections", "strategy_nt_connections")
                        self._status_row("Execution Mode", "strategy_execution_mode")
                        self._status_row("Runtime State", "strategy_runtime_state")
                        self._status_row("Intake", "strategy_intake")
                        self._status_row("Heartbeat", "strategy_heartbeat")
                        self._status_row("Recovery", "strategy_recovery")
                        self._status_row("NT Status", "strategy_nt_status")
                        self._status_row("Account", "strategy_account")
                        self._status_row("Instrument", "strategy_instrument")
                        self._status_row("Template", "strategy_template")
                        self._status_row("Signal ID", "strategy_signal_id")
                        self._status_row("Position ID", "strategy_position_id")
                        self._status_row("Position", "strategy_position")
                        self._status_row("Protective Stop", "strategy_stop_status")
                        self._status_row("Target", "strategy_target_status")
                        self._status_row("Last Event", "strategy_last_event")
                        self._status_row("Last Heartbeat", "strategy_last_heartbeat")

                    dpg.add_spacer(height=8)
                    dpg.add_separator()
                    dpg.add_spacer(height=6)
                    dpg.add_text("CONTROLS", color=MUTED)
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Enable Intake", callback=self._on_enable_intake)
                        dpg.add_button(label="Disable Intake", callback=self._on_disable_intake)
                        dpg.add_button(label="Request Snapshot", callback=self._on_request_snapshot)
                    dpg.add_spacer(height=4)
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Flatten + Disable", callback=self._on_flatten_disable)
                        dpg.add_button(label="Cancel Working", callback=self._on_cancel_working)
                        dpg.add_button(label="Acknowledge Recovery", callback=self._on_ack_recovery)
                    dpg.add_spacer(height=4)
                    with dpg.group(horizontal=True):
                        dpg.add_checkbox(
                            label="Confirm live strategy intake",
                            tag="strategy_live_confirm",
                            default_value=False,
                        )
                        dpg.add_button(label="Reset View", callback=self._on_reset_view)

                with dpg.child_window(width=-1, height=430, border=True):
                    dpg.add_text("TEST HARNESS", color=MUTED)
                    dpg.add_spacer(height=4)
                    with dpg.group(horizontal=True):
                        with dpg.group():
                            dpg.add_text("Account", color=MUTED)
                            dpg.add_input_text(
                                tag="strategy_form_account",
                                width=140,
                                default_value=self._config.bridge_default_account,
                            )
                            dpg.add_spacer(height=4)
                            dpg.add_text("Instrument", color=MUTED)
                            dpg.add_input_text(tag="strategy_form_instrument", width=140, default_value="NQ 06-26")
                            dpg.add_spacer(height=4)
                            dpg.add_text("Template", color=MUTED)
                            dpg.add_input_text(
                                tag="strategy_form_template",
                                width=180,
                                default_value="runner_reversal_template",
                            )
                            dpg.add_spacer(height=4)
                            dpg.add_text("Thesis ID", color=MUTED)
                            dpg.add_input_text(
                                tag="strategy_form_thesis",
                                width=180,
                                default_value="gui_test_signal",
                            )

                        dpg.add_spacer(width=16)
                        with dpg.group():
                            dpg.add_text("Quantity", color=MUTED)
                            dpg.add_input_int(tag="strategy_form_qty", width=100, default_value=1, min_value=1, max_value=10)
                            dpg.add_spacer(height=4)
                            dpg.add_text("Stop Ticks", color=MUTED)
                            dpg.add_input_int(
                                tag="strategy_form_stop_ticks",
                                width=100,
                                default_value=self._config.strategy_test_default_stop_ticks,
                                min_value=0,
                                max_value=500,
                            )
                            dpg.add_spacer(height=4)
                            dpg.add_text("Stop Price", color=MUTED)
                            dpg.add_input_float(tag="strategy_form_stop_price", width=120, default_value=0.0, format="%.2f")
                            dpg.add_spacer(height=4)
                            dpg.add_text("Target Ticks", color=MUTED)
                            dpg.add_input_int(
                                tag="strategy_form_target_ticks",
                                width=100,
                                default_value=self._config.strategy_test_default_target_ticks,
                                min_value=0,
                                max_value=500,
                            )

                        dpg.add_spacer(width=16)
                        with dpg.group():
                            dpg.add_text("Protective Controls", color=MUTED)
                            dpg.add_input_float(tag="strategy_move_stop_price", width=140, default_value=0.0, format="%.2f")
                            dpg.add_button(label="Move Stop", tag="strategy_move_stop_btn", callback=self._on_move_stop)
                            dpg.add_spacer(height=12)
                            dpg.add_button(label="Apply Smoke Preset", callback=self._on_apply_smoke_preset, width=160)
                            dpg.add_button(label="Apply Bracket Preset", callback=self._on_apply_bracket_preset, width=160)
                            dpg.add_spacer(height=12)
                            dpg.add_text("Heartbeat", color=MUTED)
                            dpg.add_checkbox(
                                label="Auto heartbeat",
                                tag="strategy_auto_heartbeat",
                                default_value=True,
                            )
                            dpg.add_input_int(
                                tag="strategy_heartbeat_seconds",
                                width=90,
                                default_value=self._config.strategy_test_heartbeat_seconds,
                                min_value=5,
                                max_value=300,
                            )
                            dpg.add_spacer(height=12)
                            dpg.add_text("Dispatch", color=MUTED)
                            dpg.add_button(label="Send HEARTBEAT", tag="strategy_send_heartbeat_btn", callback=self._on_send_heartbeat, width=160)
                            dpg.add_button(label="Send ENTER_LONG", tag="strategy_send_enter_long_btn", callback=self._on_send_enter_long, width=160)
                            dpg.add_button(label="Send ENTER_SHORT", tag="strategy_send_enter_short_btn", callback=self._on_send_enter_short, width=160)
                            dpg.add_button(label="Send EXIT_ALL", tag="strategy_send_exit_all_btn", callback=self._on_send_exit_all, width=160)
                            dpg.add_button(label="Send SCRATCH", tag="strategy_send_scratch_btn", callback=self._on_send_scratch, width=160)

                    dpg.add_spacer(height=10)
                    dpg.add_separator()
                    dpg.add_spacer(height=6)
                    dpg.add_text("", tag="strategy_status_message", color=WHITE, wrap=520)
                    dpg.add_spacer(height=8)
                    dpg.add_separator()
                    dpg.add_spacer(height=6)
                    dpg.add_text("SMOKE SUITE", color=MUTED)
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Run LONG Smoke", callback=self._on_run_smoke_long, width=160)
                        dpg.add_button(label="Run SHORT Smoke", callback=self._on_run_smoke_short, width=160)
                        dpg.add_button(label="Run BOTH", callback=self._on_run_smoke_both, width=120)
                    dpg.add_spacer(height=4)
                    dpg.add_text("", tag="strategy_smoke_mode_note", color=YELLOW, wrap=520)
                    dpg.add_text("", tag="strategy_smoke_status", color=WHITE, wrap=520)
                    dpg.add_text("", tag="strategy_smoke_report_path", color=MUTED, wrap=520)
                    dpg.add_text("", tag="strategy_smoke_report_summary", color=WHITE, wrap=520)

            dpg.add_spacer(height=10)
            dpg.add_separator()
            dpg.add_spacer(height=6)
            dpg.add_text("ACTIVE STRATEGY ORDERS", color=MUTED)
            dpg.add_spacer(height=4)
            with dpg.table(
                tag="strategy_orders_table",
                header_row=True,
                borders_innerV=True,
                borders_outerH=True,
                borders_outerV=True,
                borders_innerH=True,
                row_background=True,
                resizable=True,
                scrollY=True,
                height=180,
            ):
                for label, width in (
                    ("Signal", 220),
                    ("Order ID", 120),
                    ("Action", 70),
                    ("Type", 90),
                    ("Qty", 55),
                    ("Price", 85),
                    ("Stop", 85),
                    ("Status", 100),
                    ("Time", 110),
                ):
                    dpg.add_table_column(label=label, init_width_or_weight=width)
            dpg.add_spacer(height=8)
            dpg.add_separator()
            dpg.add_spacer(height=6)
            with dpg.group(horizontal=True):
                dpg.add_text("STRATEGY EVENTS", color=MUTED)
                dpg.add_spacer(width=20)
                dpg.add_checkbox(label="Active Only", tag="strategy_filter_active", default_value=False)
                dpg.add_checkbox(label="Errors Only", tag="strategy_filter_errors", default_value=False)
                dpg.add_checkbox(label="Current Signal Only", tag="strategy_filter_current_signal", default_value=False)
                dpg.add_button(label="Refresh", callback=lambda *_: self._render_events_table())
            dpg.add_spacer(height=4)
            with dpg.table(
                tag="strategy_events_table",
                header_row=True,
                borders_innerV=True,
                borders_outerH=True,
                borders_outerV=True,
                borders_innerH=True,
                row_background=True,
                resizable=True,
                scrollY=True,
                height=-1,
            ):
                for label, width in (
                    ("Time", 140),
                    ("Event", 120),
                    ("Signal ID", 170),
                    ("Account", 100),
                    ("Instrument", 110),
                    ("Runtime", 110),
                    ("Summary", 280),
                    ("Error", 140),
                ):
                    dpg.add_table_column(label=label, init_width_or_weight=width)

    @staticmethod
    def _status_row(label: str, value_tag: str) -> None:
        with dpg.table_row():
            dpg.add_text(label, color=MUTED)
            dpg.add_text("-", tag=value_tag, color=WHITE)

    def _on_enable_intake(self, sender, app_data, user_data) -> None:
        confirmed_live = bool(dpg.get_value("strategy_live_confirm"))
        ok = self._runtime.set_intake_enabled(True, confirmed_live=confirmed_live, source="gui")
        self._set_status_message("Signal intake enabled." if ok else "Enable intake rejected.", GREEN if ok else RED)
        self._runtime.request_state_snapshot(reason="operator_enable_intake")

    def _on_disable_intake(self, sender, app_data, user_data) -> None:
        ok = self._runtime.set_intake_enabled(False, source="gui")
        self._set_status_message("Signal intake disabled." if ok else "Disable intake failed.", YELLOW if ok else RED)
        self._runtime.request_state_snapshot(reason="operator_disable_intake")

    def _on_request_snapshot(self, sender, app_data, user_data) -> None:
        self._runtime.request_state_snapshot(reason="operator_refresh")
        self._set_status_message("Requested fresh runtime snapshot.", WHITE)

    def _on_flatten_disable(self, sender, app_data, user_data) -> None:
        self._submit_control_command({"command": "FLATTEN_AND_DISABLE"})

    def _on_cancel_working(self, sender, app_data, user_data) -> None:
        self._submit_control_command({"command": "CANCEL_WORKING"})

    def _on_ack_recovery(self, sender, app_data, user_data) -> None:
        ok = self._runtime.clear_recovery_if_flat(source="gui")
        self._set_status_message("Recovery cleared." if ok else "Recovery clear rejected.", GREEN if ok else RED)
        self._runtime.request_state_snapshot(reason="operator_ack_recovery")

    def _on_reset_view(self, sender, app_data, user_data) -> None:
        self._state.clear_strategy_events()
        self._runtime.request_state_snapshot(reason="operator_reset_view")
        self._set_status_message("Reset local event view and requested a fresh snapshot.", WHITE)

    def _on_move_stop(self, sender, app_data, user_data) -> None:
        stop_price = float(dpg.get_value("strategy_move_stop_price") or 0.0)
        self._submit_control_command({"command": "MOVE_STOP", "stop_price": stop_price})

    def _on_apply_smoke_preset(self, sender, app_data, user_data) -> None:
        dpg.set_value("strategy_form_stop_ticks", self._config.strategy_test_default_stop_ticks)
        dpg.set_value("strategy_form_target_ticks", self._config.strategy_test_default_target_ticks)
        dpg.set_value("strategy_heartbeat_seconds", self._config.strategy_test_heartbeat_seconds)
        self._set_status_message(
            f"Applied smoke preset: stop={self._config.strategy_test_default_stop_ticks} target={self._config.strategy_test_default_target_ticks}.",
            WHITE,
        )

    def _on_apply_bracket_preset(self, sender, app_data, user_data) -> None:
        dpg.set_value("strategy_form_stop_ticks", self._config.strategy_test_default_stop_ticks)
        dpg.set_value("strategy_form_target_ticks", self._config.strategy_test_bracket_target_ticks)
        dpg.set_value("strategy_heartbeat_seconds", self._config.strategy_test_heartbeat_seconds)
        self._set_status_message(
            f"Applied bracket preset: stop={self._config.strategy_test_default_stop_ticks} target={self._config.strategy_test_bracket_target_ticks}.",
            WHITE,
        )

    def _on_send_heartbeat(self, sender, app_data, user_data) -> None:
        self._submit_control_command({"command": "HEARTBEAT"})
        self._arm_next_heartbeat()

    def _on_send_enter_long(self, sender, app_data, user_data) -> None:
        self._submit_entry_command("ENTER_LONG")

    def _on_send_enter_short(self, sender, app_data, user_data) -> None:
        self._submit_entry_command("ENTER_SHORT")

    def _on_send_exit_all(self, sender, app_data, user_data) -> None:
        self._submit_control_command({"command": "EXIT_ALL"})

    def _on_send_scratch(self, sender, app_data, user_data) -> None:
        self._submit_control_command({"command": "SCRATCH"})

    def _on_run_smoke_long(self, sender, app_data, user_data) -> None:
        self._start_smoke_suite(["LONG"])

    def _on_run_smoke_short(self, sender, app_data, user_data) -> None:
        self._start_smoke_suite(["SHORT"])

    def _on_run_smoke_both(self, sender, app_data, user_data) -> None:
        self._start_smoke_suite(["LONG", "SHORT"])

    def _submit_entry_command(self, command: str) -> None:
        payload = self._base_form_payload()
        payload.update(
            {
                "command": command,
                "template_name": dpg.get_value("strategy_form_template").strip(),
                "quantity": int(dpg.get_value("strategy_form_qty") or 0),
                "entry_mode": "market",
                "stop_ticks": int(dpg.get_value("strategy_form_stop_ticks") or 0),
                "target_ticks": int(dpg.get_value("strategy_form_target_ticks") or 0),
                "thesis_id": dpg.get_value("strategy_form_thesis").strip(),
                "signal_expiry_seconds": self._config.bridge_stale_signal_seconds,
            }
        )
        stop_price = float(dpg.get_value("strategy_form_stop_price") or 0.0)
        if stop_price > 0:
            payload["stop_price"] = stop_price
        instruction = self._runtime.submit_operator_command(payload, source="gui")
        self._set_status_message(
            f"Submitted {command} for {payload['instrument']}." if instruction is not None else f"{command} rejected.",
            GREEN if instruction is not None else RED,
        )
        if instruction is not None:
            self._arm_next_heartbeat()

    def _submit_control_command(self, payload: dict[str, object]) -> None:
        full_payload = self._base_form_payload()
        full_payload.update(payload)
        instruction = self._runtime.submit_operator_command(full_payload, source="gui")
        command_name = str(full_payload.get("command") or "COMMAND")
        self._set_status_message(
            f"Submitted {command_name}." if instruction is not None else f"{command_name} rejected.",
            GREEN if instruction is not None else RED,
        )

    def _base_form_payload(self) -> dict[str, object]:
        snapshot = self._state.get_strategy_snapshot()
        instrument = dpg.get_value("strategy_form_instrument").strip()
        account = dpg.get_value("strategy_form_account").strip()
        if snapshot is not None:
            instrument = instrument or snapshot.instrument
            account = account or snapshot.account
        return {
            "account": account or self._config.bridge_default_account,
            "instrument": instrument,
        }

    def _set_status_message(self, message: str, color: tuple[int, int, int, int]) -> None:
        self._status_color = color
        dpg.configure_item("strategy_status_message", color=color)
        dpg.set_value("strategy_status_message", message)

    def refresh(self) -> None:
        self._refresh_status()
        self._maybe_send_auto_heartbeat()
        self._refresh_smoke_status()
        self._refresh_strategy_orders()
        if self._state.check_and_clear("strategy_dirty"):
            self._render_events_table()

    def _refresh_status(self) -> None:
        snapshot = self._state.get_strategy_snapshot()
        health = self._state.strategy_health
        if snapshot is None:
            dpg.set_value("strategy_api_endpoint", self._state.strategy_api_endpoint or f"tcp://{self._config.strategy_api_host}:{self._config.strategy_api_port}")
            dpg.set_value("strategy_api_clients", str(self._state.strategy_connected_clients))
            dpg.set_value("strategy_nt_connections", "0")
            return

        dpg.set_value("strategy_api_endpoint", self._state.strategy_api_endpoint or f"tcp://{self._config.strategy_api_host}:{self._config.strategy_api_port}")
        dpg.set_value("strategy_api_clients", str(self._state.strategy_connected_clients))
        dpg.set_value("strategy_nt_connections", str(snapshot.nt_connection_count))
        dpg.set_value("strategy_execution_mode", snapshot.execution_mode)
        dpg.set_value("strategy_runtime_state", snapshot.runtime_state)
        dpg.set_value("strategy_intake", "Enabled" if snapshot.intake_enabled else "Disabled")
        dpg.set_value("strategy_heartbeat", "Faulted" if snapshot.heartbeat_faulted else "Healthy")
        dpg.set_value("strategy_recovery", snapshot.recovery_reason if snapshot.recovery_required else "Clear")
        dpg.set_value("strategy_nt_status", "Connected" if snapshot.nt_connected else "Disconnected")
        dpg.set_value("strategy_account", snapshot.account or self._config.bridge_default_account)
        dpg.set_value("strategy_instrument", snapshot.instrument or "-")
        dpg.set_value("strategy_template", str(snapshot.details.get("active_template") or "-"))
        dpg.set_value("strategy_signal_id", snapshot.signal_id or "-")
        dpg.set_value("strategy_position_id", snapshot.position_id or "-")
        dpg.set_value(
            "strategy_position",
            f"{snapshot.position_side} qty={abs(snapshot.position_quantity)} avg={snapshot.average_price:.2f}",
        )
        dpg.set_value(
            "strategy_stop_status",
            f"{float(snapshot.details.get('pending_stop_price') or 0.0):.2f}" if float(snapshot.details.get("pending_stop_price") or 0.0) > 0 else "-",
        )
        dpg.set_value(
            "strategy_target_status",
            (
                f"Disabled ({int(snapshot.details.get('pending_target_ticks') or 0)} ticks)"
                if int(snapshot.details.get("pending_target_ticks") or 0) <= 0
                else f"{int(snapshot.details.get('pending_target_ticks') or 0)} ticks"
            ),
        )
        dpg.set_value("strategy_last_event", self._format_timestamp(self._state.strategy_last_event_at))
        dpg.set_value("strategy_last_heartbeat", self._format_timestamp(self._state.strategy_last_heartbeat_at))

        warning_lines: list[str] = []
        if not snapshot.nt_connected:
            warning_lines.append("NinjaTrader is disconnected.")
        elif snapshot.nt_connection_count != 1:
            warning_lines.append(
                f"Expected exactly one NinjaTrader connection for strategy testing; found {snapshot.nt_connection_count}."
            )
        if snapshot.heartbeat_faulted:
            warning_lines.append("Heartbeat timeout has faulted intake.")
        if snapshot.recovery_required:
            warning_lines.append(f"Recovery required: {snapshot.recovery_reason or 'flat and acknowledge before re-entry'}")
        if snapshot.protective_orders_faulted:
            warning_lines.append("Protective orders are missing or not acknowledged.")
        cleanup_pending_count = int(snapshot.details.get("cleanup_pending_count") or 0)
        if cleanup_pending_count > 0:
            warning_lines.append(f"{cleanup_pending_count} protective order(s) still pending cleanup after flat.")
        if snapshot.daily_lockout:
            warning_lines.append("Daily lockout is active.")
        if snapshot.execution_mode == "LIVE":
            if not snapshot.live_confirmation_received:
                warning_lines.append("Live mode requires operator confirmation before enabling intake.")
            dpg.configure_item("strategy_live_badge", color=RED)
            dpg.set_value("strategy_live_badge", "LIVE MODE")
        else:
            dpg.configure_item("strategy_live_badge", color=GREEN)
            dpg.set_value("strategy_live_badge", "SIM MODE")
        dpg.set_value("strategy_warning_text", "\n".join(warning_lines) if warning_lines else "No active runtime warnings.")
        dpg.configure_item("strategy_warning_text", color=RED if warning_lines else GREEN)

        can_move_stop = (
            snapshot.nt_connected
            and snapshot.nt_connection_count == 1
            and snapshot.position_quantity != 0
            and snapshot.runtime_state in {"InPosition", "Disabled"}
        )
        dpg.configure_item("strategy_move_stop_btn", enabled=can_move_stop)
        can_submit_entries = (
            snapshot.nt_connected
            and snapshot.nt_connection_count == 1
            and not snapshot.recovery_required
            and snapshot.runtime_state == "Idle"
        )
        dpg.configure_item("strategy_send_enter_long_btn", enabled=can_submit_entries)
        dpg.configure_item("strategy_send_enter_short_btn", enabled=can_submit_entries)
        dpg.configure_item("strategy_send_heartbeat_btn", enabled=snapshot.nt_connected)
        dpg.configure_item("strategy_send_exit_all_btn", enabled=snapshot.nt_connected)
        dpg.configure_item("strategy_send_scratch_btn", enabled=snapshot.nt_connected)

        enable_requires_confirm = snapshot.execution_mode == "LIVE" and not snapshot.live_confirmation_received
        dpg.configure_item(
            "strategy_live_confirm",
            show=snapshot.execution_mode == "LIVE",
        )
        dpg.configure_item(
            "strategy_status_message",
            color=self._status_color if self._status_color else (RED if health.strategy_fault else WHITE),
        )
        if enable_requires_confirm and not dpg.get_value("strategy_live_confirm"):
            dpg.configure_item("strategy_status_message", color=YELLOW)
        current_target_ticks = int(dpg.get_value("strategy_form_target_ticks") or 0)
        if current_target_ticks <= 0:
            dpg.set_value("strategy_smoke_mode_note", "Current form target is disabled. Use Bracket Preset or set Target Ticks > 0 to verify profit-target submission.")
            dpg.configure_item("strategy_smoke_mode_note", color=YELLOW)
        else:
            dpg.set_value("strategy_smoke_mode_note", f"Current form target is enabled at {current_target_ticks} ticks.")
            dpg.configure_item("strategy_smoke_mode_note", color=GREEN)

    def _maybe_send_auto_heartbeat(self) -> None:
        snapshot = self._state.get_strategy_snapshot()
        if snapshot is None:
            return
        if not dpg.get_value("strategy_auto_heartbeat"):
            return
        if not snapshot.nt_connected:
            return
        if snapshot.nt_connection_count != 1:
            return
        if not (snapshot.intake_enabled or snapshot.runtime_state in {"EntryPending", "InPosition"}):
            return
        now = time.monotonic()
        if now < self._next_heartbeat_monotonic:
            return
        self._runtime.submit_operator_command({"command": "HEARTBEAT"}, source="gui_auto")
        self._arm_next_heartbeat()

    def _arm_next_heartbeat(self) -> None:
        interval = int(dpg.get_value("strategy_heartbeat_seconds") or self._config.strategy_test_heartbeat_seconds)
        interval = max(5, interval)
        self._next_heartbeat_monotonic = time.monotonic() + interval

    def _start_smoke_suite(self, sides: list[str]) -> None:
        with self._smoke_lock:
            if self._smoke_running:
                self._set_status_message("A smoke suite is already running.", YELLOW)
                return
            self._smoke_running = True
            self._smoke_status = f"Running smoke suite for {', '.join(sides)}..."
            self._smoke_report_path = ""
            self._smoke_report_summary = ""
        worker = threading.Thread(
            target=self._run_smoke_suite_worker,
            args=(tuple(sides), self._capture_smoke_settings()),
            daemon=True,
            name="StrategySmokeSuite",
        )
        worker.start()
        self._set_status_message(self._smoke_status, WHITE)

    def _capture_smoke_settings(self) -> dict[str, object]:
        return {
            "account": dpg.get_value("strategy_form_account").strip() or self._config.bridge_default_account,
            "instrument": dpg.get_value("strategy_form_instrument").strip() or "NQ 06-26",
            "quantity": int(dpg.get_value("strategy_form_qty") or 1),
            "stop_ticks": int(dpg.get_value("strategy_form_stop_ticks") or 0),
            "target_ticks": int(dpg.get_value("strategy_form_target_ticks") or 0),
            "template": dpg.get_value("strategy_form_template").strip() or "runner_reversal_template",
            "heartbeat_seconds": int(dpg.get_value("strategy_heartbeat_seconds") or self._config.strategy_test_heartbeat_seconds),
        }

    def _run_smoke_suite_worker(self, sides: tuple[str, ...], options: dict[str, object]) -> None:
        started = datetime.now(timezone.utc)
        report_dir = Path(self._config.strategy_test_report_dir)
        log_file = self._config.log_dir / "ninja_account_manager.log"
        reports: list[dict[str, object]] = []
        overall_code = 0
        try:
            for side in sides:
                settings = SmokeSettings(
                    account=str(options["account"]),
                    instrument=str(options["instrument"]),
                    side=side,
                    quantity=int(options["quantity"]),
                    stop_ticks=int(options["stop_ticks"]),
                    target_ticks=int(options["target_ticks"]),
                    template=str(options["template"]),
                    thesis_id=f"gui_smoke_{side.lower()}",
                    heartbeat_seconds=float(options["heartbeat_seconds"]),
                    cleanup_command="EXIT_ALL",
                    report_dir=report_dir,
                    label=f"gui_{side.lower()}",
                    log_file=log_file,
                )
                code, report = run_smoke(settings)
                report_path = _report_path(settings, datetime.fromisoformat(str(report["run_started_at_utc"])))
                _write_report(report_path, report)
                reports.append(report)
                overall_code = max(overall_code, code)
                with self._smoke_lock:
                    self._smoke_report_path = str(report_path)
                    self._smoke_report_summary = (
                        f"{side}: {report.get('status')} | target_ticks={settings.target_ticks} | "
                        f"cleanup={report.get('cleanup_signal_id') or '-'}"
                    )
                    self._smoke_status = f"Completed {side} smoke."
                if code != 0:
                    break
                time.sleep(1.0)

            suite_path = report_dir / f"{started.strftime('%Y%m%d_%H%M%S')}_gui_strategy_suite.json"
            suite_payload = {
                "run_started_at_utc": started.isoformat(),
                "run_finished_at_utc": datetime.now(timezone.utc).isoformat(),
                "overall_code": overall_code,
                "reports": reports,
            }
            _write_report(suite_path, suite_payload)
            with self._smoke_lock:
                self._smoke_report_path = str(suite_path)
                if overall_code == 0:
                    self._smoke_status = f"Smoke suite passed for {', '.join(sides)}."
                else:
                    last_failure = next((report.get("failure_reason") for report in reports if report.get("failure_reason")), "")
                    self._smoke_status = f"Smoke suite failed: {last_failure or 'see report'}"
                self._smoke_report_summary = json.dumps(
                    {
                        "overall_code": overall_code,
                        "scenarios": [report.get("status") for report in reports],
                    }
                )
        except Exception as exc:  # noqa: BLE001
            with self._smoke_lock:
                self._smoke_status = f"Smoke suite crashed: {exc}"
                self._smoke_report_summary = ""
        finally:
            with self._smoke_lock:
                self._smoke_running = False

    def _refresh_smoke_status(self) -> None:
        with self._smoke_lock:
            running = self._smoke_running
            status = self._smoke_status
            report_path = self._smoke_report_path
            summary = self._smoke_report_summary
        dpg.set_value("strategy_smoke_status", status)
        dpg.configure_item("strategy_smoke_status", color=YELLOW if running else WHITE)
        dpg.set_value("strategy_smoke_report_path", report_path or "")
        dpg.set_value("strategy_smoke_report_summary", summary or "")

    def _visible_strategy_orders(self) -> list[Order]:
        nonterminal_statuses = {
            "Initialized",
            "Submitted",
            "Accepted",
            "Working",
            "CancelPending",
            "CancelSubmitted",
            "PartiallyFilled",
            "PartFilled",
        }
        orders = [
            order
            for order in self._state.get_orders()
            if (order.signal_name or "").startswith("TF_") and order.status in nonterminal_statuses
        ]
        orders.sort(key=lambda item: item.timestamp, reverse=True)
        return orders[:14]

    def _refresh_strategy_orders(self) -> None:
        orders = self._visible_strategy_orders()
        signature = json.dumps(
            [
                {
                    "order_id": order.order_id,
                    "signal_name": order.signal_name,
                    "status": order.status,
                    "filled_quantity": order.filled_quantity,
                    "timestamp": order.timestamp,
                }
                for order in orders
            ],
            sort_keys=True,
        )
        if signature == self._last_strategy_orders_signature:
            return
        self._last_strategy_orders_signature = signature
        for child in dpg.get_item_children("strategy_orders_table", slot=1):
            dpg.delete_item(child)
        for order in orders:
            color = _ORDER_COLORS.get(order.status, WHITE)
            with dpg.table_row(parent="strategy_orders_table"):
                dpg.add_text((order.signal_name or "-")[:32], color=WHITE)
                dpg.add_text((order.order_id or "-")[:12], color=WHITE)
                dpg.add_text(order.action or "-", color=GREEN if order.action == "Buy" else RED if order.action == "Sell" else WHITE)
                dpg.add_text(order.order_type or "-", color=WHITE)
                dpg.add_text(str(order.quantity), color=WHITE)
                dpg.add_text(f"{order.price:.2f}" if order.price else "-", color=WHITE)
                dpg.add_text(f"{order.stop_price:.2f}" if order.stop_price else "-", color=WHITE)
                dpg.add_text(order.status or "-", color=color)
                dpg.add_text(self._format_timestamp(order.timestamp), color=WHITE)

    def _render_events_table(self) -> None:
        snapshot = self._state.get_strategy_snapshot()
        current_signal_id = snapshot.signal_id if snapshot is not None else ""
        events = self._state.get_strategy_events()
        if dpg.get_value("strategy_filter_errors"):
            events = [event for event in events if event.event in {"ERROR", "REJECTED", "HEARTBEAT_TIMEOUT"}]
        if dpg.get_value("strategy_filter_current_signal") and current_signal_id:
            events = [event for event in events if event.signal_id == current_signal_id]
        if dpg.get_value("strategy_filter_active"):
            events = [
                event for event in events
                if event.runtime_state in {"EntryPending", "InPosition", "Disabled", "RecoveryRequired"}
            ]

        for child in dpg.get_item_children("strategy_events_table", slot=1):
            dpg.delete_item(child)

        for event in events:
            color = _EVENT_COLORS.get(event.event, WHITE)
            with dpg.table_row(parent="strategy_events_table"):
                dpg.add_text(self._format_timestamp(event.timestamp), color=WHITE)
                dpg.add_text(event.event, color=color)
                dpg.add_text(event.signal_id[:24] if event.signal_id else "-", color=WHITE)
                dpg.add_text(event.account or "-", color=WHITE)
                dpg.add_text(event.instrument or "-", color=WHITE)
                dpg.add_text(event.runtime_state or "-", color=WHITE)
                dpg.add_text(event.summary or "-", color=WHITE, wrap=260)
                dpg.add_text(event.error_code or "-", color=color)

    @staticmethod
    def _format_timestamp(value: str) -> str:
        if not value:
            return "-"
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value[:19]
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.strftime("%H:%M:%S")
