"""
Orders tab
==========
• Live orders table (working / filled / cancelled)
• Order entry form (market, limit, stop, stop-limit)
• Cancel-order button per working row
"""
from __future__ import annotations
import logging

import dearpygui.dearpygui as dpg

from core.data_models import Order
from core.nt_client import NinjaTraderClient
from core.state import AppState
from gui.dashboard import GREEN, RED, YELLOW, MUTED, WHITE, _pnl_color

logger = logging.getLogger(__name__)

_ORDER_STATUS_COLOR = {
    "Working":         YELLOW,
    "Filled":          GREEN,
    "Cancelled":       (140, 140, 140, 255),
    "Rejected":        RED,
    "PartiallyFilled": (180, 150, 50, 255),
}

_COLUMNS = [
    ("Order ID",       110),
    ("Account",        110),
    ("Instrument",     110),
    ("Action",          70),
    ("Type",            80),
    ("Qty",             60),
    ("Filled",          60),
    ("Price",           90),
    ("Stop",            90),
    ("Status",          90),
    ("Time",           140),
    ("",                70),   # Cancel button column
]


class OrdersPanel:
    def __init__(
        self, tab_bar: int | str, state: AppState, nt_client: NinjaTraderClient
    ) -> None:
        self._state = state
        self._nt = nt_client
        self._build(tab_bar)

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self, tab_bar: int | str) -> None:
        with dpg.tab(label="  Orders  ", parent=tab_bar, tag="tab_orders"):
            dpg.add_spacer(height=6)

            # ── Order entry form ──────────────────────────────────────────────
            with dpg.collapsing_header(label="Order Entry", default_open=True):
                dpg.add_spacer(height=4)
                with dpg.group(horizontal=True):
                    # Left column
                    with dpg.group():
                        dpg.add_text("Symbol", color=MUTED)
                        dpg.add_input_text(
                            tag="oe_symbol", default_value="ES 03-25", width=140
                        )
                        dpg.add_spacer(height=4)
                        dpg.add_text("Account", color=MUTED)
                        dpg.add_input_text(
                            tag="oe_account", default_value="Sim101", width=140
                        )

                    dpg.add_spacer(width=16)

                    # Middle column
                    with dpg.group():
                        dpg.add_text("Quantity", color=MUTED)
                        dpg.add_input_int(
                            tag="oe_qty", default_value=1,
                            min_value=1, max_value=9999, width=100,
                        )
                        dpg.add_spacer(height=4)
                        dpg.add_text("Order Type", color=MUTED)
                        dpg.add_combo(
                            ["Market", "Limit", "Stop", "StopLimit"],
                            tag="oe_type", default_value="Market", width=120,
                            callback=self._on_type_changed,
                        )

                    dpg.add_spacer(width=16)

                    # Right column (prices – hidden until needed)
                    with dpg.group(tag="oe_price_group"):
                        dpg.add_text("Limit Price", color=MUTED)
                        dpg.add_input_float(
                            tag="oe_price", default_value=0.0,
                            format="%.2f", width=110,
                        )
                        dpg.add_spacer(height=4)
                        dpg.add_text("Stop Price", color=MUTED)
                        dpg.add_input_float(
                            tag="oe_stop_price", default_value=0.0,
                            format="%.2f", width=110,
                        )

                dpg.add_spacer(height=10)

                with dpg.group(horizontal=True):
                    dpg.add_button(
                        label="  BUY / LONG  ", tag="oe_buy_btn",
                        width=150, height=32,
                        callback=self._on_buy,
                    )
                    dpg.add_spacer(width=10)
                    dpg.add_button(
                        label=" SELL / SHORT  ", tag="oe_sell_btn",
                        width=150, height=32,
                        callback=self._on_sell,
                    )

                # Colour the buy/sell buttons distinctly
                with dpg.theme() as buy_theme:
                    with dpg.theme_component(dpg.mvButton):
                        dpg.add_theme_color(dpg.mvThemeCol_Button,        (20, 130, 60, 255))
                        dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,  (30, 160, 80, 255))
                        dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,   (15, 100, 45, 255))
                dpg.bind_item_theme("oe_buy_btn", buy_theme)

                with dpg.theme() as sell_theme:
                    with dpg.theme_component(dpg.mvButton):
                        dpg.add_theme_color(dpg.mvThemeCol_Button,        (160, 30, 30, 255))
                        dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,  (190, 50, 50, 255))
                        dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,   (120, 20, 20, 255))
                dpg.bind_item_theme("oe_sell_btn", sell_theme)

                dpg.add_spacer(height=6)
                dpg.add_text("", tag="oe_status_msg", color=YELLOW)

            dpg.add_spacer(height=8)
            dpg.add_separator()
            dpg.add_spacer(height=6)

            # ── Orders table ──────────────────────────────────────────────────
            dpg.add_text("Orders", color=MUTED)
            dpg.add_spacer(height=4)
            with dpg.table(
                tag="orders_table",
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

        # Initially hide price fields for Market orders
        self._on_type_changed(None, "Market", None)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _on_type_changed(self, sender, app_data, user_data) -> None:
        show = app_data in ("Limit", "Stop", "StopLimit")
        dpg.configure_item("oe_price_group", show=show)

    def _on_buy(self, sender, app_data, user_data) -> None:
        self._submit_order("Buy")

    def _on_sell(self, sender, app_data, user_data) -> None:
        self._submit_order("Sell")

    def _submit_order(self, action: str) -> None:
        symbol     = dpg.get_value("oe_symbol").strip()
        account    = dpg.get_value("oe_account").strip()
        qty        = dpg.get_value("oe_qty")
        order_type = dpg.get_value("oe_type")
        price      = dpg.get_value("oe_price")
        stop_price = dpg.get_value("oe_stop_price")

        if not symbol or qty < 1:
            dpg.set_value("oe_status_msg", "Error: Symbol and quantity are required.")
            return

        if not self._nt.is_connected:
            dpg.set_value("oe_status_msg", "Error: No NinjaTrader connection.")
            return

        self._nt.submit_order(account, symbol, action, order_type, qty, price, stop_price)
        dpg.set_value(
            "oe_status_msg",
            f"Submitted: {action} {qty}x {symbol} @ {order_type}",
        )
        logger.info("User submitted %s %s %s x%d", action, order_type, symbol, qty)

    def _on_cancel(self, sender, app_data, order_id: str) -> None:
        if order_id:
            self._nt.cancel_order(order_id)
            dpg.set_value("oe_status_msg", f"Cancel sent for order {order_id}")

    # ── Refresh ───────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        if not self._state.check_and_clear("orders_dirty"):
            return
        _clear_table("orders_table")
        orders = sorted(
            self._state.get_orders(),
            key=lambda o: o.timestamp,
            reverse=True,
        )
        for order in orders:
            status_color = _ORDER_STATUS_COLOR.get(order.status, WHITE)
            with dpg.table_row(parent="orders_table"):
                dpg.add_text(order.order_id[:10],     color=WHITE)
                dpg.add_text(order.account,            color=WHITE)
                dpg.add_text(order.instrument,         color=WHITE)
                act_color = GREEN if order.action == "Buy" else RED
                dpg.add_text(order.action,             color=act_color)
                dpg.add_text(order.order_type,         color=WHITE)
                dpg.add_text(str(order.quantity),      color=WHITE)
                dpg.add_text(str(order.filled_quantity), color=WHITE)
                dpg.add_text(
                    f"{order.price:.2f}" if order.price else "MKT", color=WHITE
                )
                dpg.add_text(
                    f"{order.stop_price:.2f}" if order.stop_price else "—", color=WHITE
                )
                dpg.add_text(order.status, color=status_color)
                dpg.add_text(order.timestamp[:19],     color=WHITE)
                if order.status == "Working":
                    dpg.add_button(
                        label="Cancel",
                        callback=self._on_cancel,
                        user_data=order.order_id,
                        width=60,
                    )
                else:
                    dpg.add_text("")


def _clear_table(tag: str) -> None:
    for child in dpg.get_item_children(tag, slot=1):
        dpg.delete_item(child)
