"""Dashboard tab – summary cards and connection status."""
from __future__ import annotations

import dearpygui.dearpygui as dpg

from core.state import AppState

# Colour palette (reused across all panels)
GREEN = (50, 220, 110, 255)
RED = (220, 60, 60, 255)
YELLOW = (220, 200, 50, 255)
MUTED = (140, 140, 160, 255)
WHITE = (220, 220, 230, 255)


def _pnl_color(value: float) -> tuple[int, int, int, int]:
    return GREEN if value >= 0 else RED


class DashboardPanel:
    def __init__(self, tab_bar: int | str, state: AppState) -> None:
        self._state = state
        self._build(tab_bar)

    # ── Build (runs once on startup) ──────────────────────────────────────────

    def _build(self, tab_bar: int | str) -> None:
        with dpg.tab(label="  Dashboard  ", parent=tab_bar, tag="tab_dashboard"):
            dpg.add_spacer(height=6)

            # ── Connection status bar ─────────────────────────────────────────
            with dpg.group(horizontal=True):
                dpg.add_text("Server:", color=MUTED)
                dpg.add_text("STARTING", tag="dash_server_status", color=YELLOW)
                dpg.add_spacer(width=24)
                dpg.add_text("NinjaTrader:", color=MUTED)
                dpg.add_text("DISCONNECTED", tag="dash_nt_status", color=RED)
                dpg.add_spacer(width=24)
                dpg.add_text("Clients connected:", color=MUTED)
                dpg.add_text("0", tag="dash_client_count", color=WHITE)

            dpg.add_spacer(height=6)
            dpg.add_separator()
            dpg.add_spacer(height=10)

            # ── Summary cards ─────────────────────────────────────────────────
            with dpg.group(horizontal=True):
                self._card("BALANCE",         "dash_balance",     "$0.00",   False)
                dpg.add_spacer(width=10)
                self._card("REALIZED P&L",    "dash_rpnl",        "$0.00",   True)
                dpg.add_spacer(width=10)
                self._card("UNREALIZED P&L",  "dash_upnl",        "$0.00",   True)
                dpg.add_spacer(width=10)
                self._card("BUYING POWER",    "dash_buypower",    "$0.00",   False)
                dpg.add_spacer(width=10)
                self._card("OPEN POSITIONS",  "dash_pos_count",   "0",       False)
                dpg.add_spacer(width=10)
                self._card("ACTIVE ORDERS",   "dash_ord_count",   "0",       False)

            dpg.add_spacer(height=14)
            dpg.add_separator()
            dpg.add_spacer(height=8)

            # ── Market data quick-view ────────────────────────────────────────
            dpg.add_text("MARKET DATA", color=MUTED)
            dpg.add_spacer(height=4)
            with dpg.table(
                tag="dash_md_table",
                header_row=True,
                borders_innerV=True,
                borders_outerH=True,
                borders_outerV=True,
                row_background=True,
                height=160,
                scrollY=True,
            ):
                for label in ("Instrument", "Bid", "Ask", "Last", "Volume"):
                    dpg.add_table_column(label=label)

    @staticmethod
    def _card(header: str, tag: str, default: str, pnl_colored: bool) -> None:
        with dpg.child_window(width=170, height=80, border=True):
            dpg.add_text(header, color=MUTED)
            dpg.add_spacer(height=6)
            dpg.add_text(default, tag=tag, color=WHITE)

    # ── Refresh (called every render frame) ───────────────────────────────────

    def refresh(self) -> None:
        state = self._state

        # Header dot (always updated, not guarded by a dirty flag)
        if state.nt_connected:
            dpg.configure_item("hdr_status_dot",   color=(50, 220, 110, 255))
            dpg.set_value("hdr_status_label", "NT8 connected")
        else:
            dpg.configure_item("hdr_status_dot",   color=(220, 60, 60, 255))
            dpg.set_value("hdr_status_label", "waiting for NinjaTrader…")

        # Connection status
        if state.is_server_running:
            dpg.configure_item("dash_server_status", default_value="LISTENING", color=GREEN)
        else:
            dpg.configure_item("dash_server_status", default_value="STARTING", color=YELLOW)

        if state.nt_connected:
            dpg.configure_item("dash_nt_status", default_value="CONNECTED", color=GREEN)
        else:
            dpg.configure_item("dash_nt_status", default_value="DISCONNECTED", color=RED)

        # Account summary
        accounts = state.get_accounts()
        total_balance   = sum(a.balance       for a in accounts)
        total_rpnl      = sum(a.realized_pnl  for a in accounts)
        total_upnl      = sum(a.unrealized_pnl for a in accounts)
        total_buypower  = sum(a.buying_power   for a in accounts)

        dpg.set_value("dash_balance",  f"${total_balance:>12,.2f}")
        dpg.configure_item("dash_rpnl",   default_value=f"${total_rpnl:>12,.2f}",  color=_pnl_color(total_rpnl))
        dpg.configure_item("dash_upnl",   default_value=f"${total_upnl:>12,.2f}",  color=_pnl_color(total_upnl))
        dpg.set_value("dash_buypower", f"${total_buypower:>12,.2f}")

        positions    = state.get_positions()
        active_orders = [o for o in state.get_orders() if o.status == "Working"]
        dpg.set_value("dash_pos_count", str(len(positions)))
        dpg.set_value("dash_ord_count", str(len(active_orders)))

        # Market data table
        if state.check_and_clear("market_data_dirty"):
            _clear_table("dash_md_table")
            for md in state.get_market_data():
                spread = md.ask - md.bid
                with dpg.table_row(parent="dash_md_table"):
                    dpg.add_text(md.instrument)
                    dpg.add_text(f"{md.bid:.4f}")
                    dpg.add_text(f"{md.ask:.4f}")
                    dpg.add_text(f"{md.last:.4f}")
                    dpg.add_text(f"{md.volume:,}")


def _clear_table(tag: str) -> None:
    for child in dpg.get_item_children(tag, slot=1):
        dpg.delete_item(child)
