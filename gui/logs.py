"""Logs tab – scrollable, filterable application log viewer."""
from __future__ import annotations

import dearpygui.dearpygui as dpg

from core.state import AppState
from gui.dashboard import MUTED, WHITE

_LEVEL_COLORS = {
    "[ERROR]":   (220,  60,  60, 255),
    "[WARNING]": (220, 180,  50, 255),
    "[INFO]":    (160, 200, 255, 255),
    "[ACCOUNT]": ( 80, 200, 130, 255),
    "[POSITION]":( 80, 200, 130, 255),
    "[ORDER]":   (200, 160,  50, 255),
    "[CONNECTION]": (180, 130, 255, 255),
    "[SERVER]":  (130, 180, 255, 255),
}


class LogsPanel:
    def __init__(self, tab_bar: int | str, state: AppState) -> None:
        self._state = state
        self._filter_text = ""
        self._auto_scroll = True
        self._build(tab_bar)

    def _build(self, tab_bar: int | str) -> None:
        with dpg.tab(label="  Logs  ", parent=tab_bar, tag="tab_logs"):
            dpg.add_spacer(height=6)
            with dpg.group(horizontal=True):
                dpg.add_text("Filter:", color=MUTED)
                dpg.add_input_text(
                    tag="log_filter",
                    hint="type to filter…",
                    width=280,
                    callback=self._on_filter_change,
                )
                dpg.add_spacer(width=16)
                dpg.add_checkbox(
                    label="Auto-scroll",
                    tag="log_autoscroll_cb",
                    default_value=True,
                    callback=lambda s, v, u: setattr(self, "_auto_scroll", v),
                )
                dpg.add_spacer(width=16)
                dpg.add_button(label="Clear", width=70, callback=self._on_clear)

            dpg.add_spacer(height=6)
            with dpg.child_window(
                tag="log_scroll_area",
                height=-1,
                horizontal_scrollbar=False,
                border=True,
            ):
                dpg.add_text(
                    "— Application log —",
                    tag="log_content_text",
                    wrap=1400,
                    color=MUTED,
                )

    def _on_filter_change(self, sender, app_data, user_data) -> None:
        self._filter_text = app_data.strip().lower()
        # Force redraw
        with self._state._lock:
            self._state.logs_dirty = True

    def _on_clear(self, sender, app_data, user_data) -> None:
        with self._state._lock:
            self._state.log_lines.clear()
            self._state.logs_dirty = True

    def refresh(self) -> None:
        if not self._state.check_and_clear("logs_dirty"):
            return
        lines = self._state.get_logs()
        if self._filter_text:
            lines = [l for l in lines if self._filter_text in l.lower()]
        text = "\n".join(lines)
        dpg.set_value("log_content_text", text or "— no entries —")
        if self._auto_scroll:
            dpg.set_y_scroll("log_scroll_area", dpg.get_y_scroll_max("log_scroll_area"))
