"""Accounts tab – detailed per-account breakdown."""
from __future__ import annotations

import dearpygui.dearpygui as dpg

from core.state import AppState
from gui.dashboard import GREEN, RED, MUTED, WHITE, _pnl_color

_COLUMNS = [
    ("Account",            120),
    ("Balance",            120),
    ("Cash Value",         120),
    ("Realized P&L",       120),
    ("Unrealized P&L",     130),
    ("Initial Margin",     120),
    ("Maint. Margin",      120),
    ("Buying Power",       120),
    ("Excess",             110),
]


class AccountsPanel:
    def __init__(self, tab_bar: int | str, state: AppState) -> None:
        self._state = state
        self._build(tab_bar)

    def _build(self, tab_bar: int | str) -> None:
        with dpg.tab(label="  Accounts  ", parent=tab_bar, tag="tab_accounts"):
            dpg.add_spacer(height=6)
            dpg.add_text("Account Details", color=MUTED)
            dpg.add_spacer(height=4)
            with dpg.table(
                tag="accounts_table",
                header_row=True,
                borders_innerV=True,
                borders_outerH=True,
                borders_outerV=True,
                borders_innerH=True,
                row_background=True,
                resizable=True,
                scrollX=True,
                scrollY=True,
                height=-1,
            ):
                for label, width in _COLUMNS:
                    dpg.add_table_column(label=label, init_width_or_weight=width)

    def refresh(self) -> None:
        if not self._state.check_and_clear("accounts_dirty"):
            return
        _clear_table("accounts_table")
        for acc in self._state.get_accounts():
            with dpg.table_row(parent="accounts_table"):
                dpg.add_text(acc.name,              color=WHITE)
                dpg.add_text(f"${acc.balance:,.2f}", color=WHITE)
                dpg.add_text(f"${acc.cash_value:,.2f}", color=WHITE)
                dpg.add_text(f"${acc.realized_pnl:,.2f}",   color=_pnl_color(acc.realized_pnl))
                dpg.add_text(f"${acc.unrealized_pnl:,.2f}", color=_pnl_color(acc.unrealized_pnl))
                dpg.add_text(f"${acc.initial_margin:,.2f}", color=WHITE)
                dpg.add_text(f"${acc.maintenance_margin:,.2f}", color=WHITE)
                dpg.add_text(f"${acc.buying_power:,.2f}", color=WHITE)
                dpg.add_text(f"${acc.excess:,.2f}",       color=_pnl_color(acc.excess))


def _clear_table(tag: str) -> None:
    for child in dpg.get_item_children(tag, slot=1):
        dpg.delete_item(child)
