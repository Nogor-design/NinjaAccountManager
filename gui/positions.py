"""Positions tab – open positions with real-time P&L."""
from __future__ import annotations

import dearpygui.dearpygui as dpg

from core.state import AppState
from gui.dashboard import GREEN, RED, MUTED, WHITE, _pnl_color

_COLUMNS = [
    ("Account",         120),
    ("Instrument",      130),
    ("Qty",              70),
    ("Direction",        90),
    ("Avg Price",       110),
    ("Unrealized P&L",  130),
    ("Market Value",    120),
]


class PositionsPanel:
    def __init__(self, tab_bar: int | str, state: AppState) -> None:
        self._state = state
        self._build(tab_bar)

    def _build(self, tab_bar: int | str) -> None:
        with dpg.tab(label="  Positions  ", parent=tab_bar, tag="tab_positions"):
            dpg.add_spacer(height=6)
            dpg.add_text("Open Positions", color=MUTED)
            dpg.add_spacer(height=4)
            with dpg.table(
                tag="positions_table",
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
                for label, width in _COLUMNS:
                    dpg.add_table_column(label=label, init_width_or_weight=width)

    def refresh(self) -> None:
        if not self._state.check_and_clear("positions_dirty"):
            return
        _clear_table("positions_table")
        for pos in self._state.get_positions():
            direction = "LONG" if pos.quantity > 0 else "SHORT"
            dir_color = GREEN if pos.quantity > 0 else RED
            with dpg.table_row(parent="positions_table"):
                dpg.add_text(pos.account,                  color=WHITE)
                dpg.add_text(pos.instrument,               color=WHITE)
                dpg.add_text(str(abs(pos.quantity)),       color=WHITE)
                dpg.add_text(direction,                    color=dir_color)
                dpg.add_text(f"{pos.avg_price:.4f}",       color=WHITE)
                dpg.add_text(
                    f"${pos.unrealized_pnl:,.2f}",
                    color=_pnl_color(pos.unrealized_pnl),
                )
                dpg.add_text(f"${pos.market_value:,.2f}",  color=WHITE)


def _clear_table(tag: str) -> None:
    for child in dpg.get_item_children(tag, slot=1):
        dpg.delete_item(child)
