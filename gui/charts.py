"""
Charts tab
==========
Real-time candlestick chart with optional SMA/EMA overlays.
Candle data is fed by the BAR_UPDATE event from NinjaTrader.
"""
from __future__ import annotations
import logging
import time
from datetime import datetime

import dearpygui.dearpygui as dpg

from core.config import AppConfig
from core.data_models import Candle
from core.nt_client import NinjaTraderClient
from core.state import AppState
from gui.dashboard import MUTED, WHITE, GREEN, RED

logger = logging.getLogger(__name__)


def _sma(values: list[float], period: int) -> list[float]:
    result = [float("nan")] * len(values)
    for i in range(period - 1, len(values)):
        result[i] = sum(values[i - period + 1 : i + 1]) / period
    return result


def _ema(values: list[float], period: int) -> list[float]:
    result = [float("nan")] * len(values)
    k = 2 / (period + 1)
    prev = None
    for i, v in enumerate(values):
        if i < period - 1:
            continue
        if prev is None:
            prev = sum(values[:period]) / period
        prev = v * k + prev * (1 - k)
        result[i] = prev
    return result


class ChartsPanel:
    # Bar period (minutes) → candle weight so each candle fills ~80 % of its slot.
    # Formula:  weight = 0.8 * period_minutes
    # DPG uses  actual_body_width_seconds = weight * mvTimeUnit_Min_seconds (60)
    # e.g. 1-min bars → weight=0.8 → body=48 s, gap=12 s (80 % fill)
    _TF_PERIOD: dict[str, int] = {
        "1 Minute": 1, "5 Minutes": 5, "15 Minutes": 15, "1 Hour": 60
    }

    def __init__(
        self,
        tab_bar: int | str,
        state: AppState,
        nt_client: NinjaTraderClient,
        config: AppConfig,
    ) -> None:
        self._state = state
        self._nt = nt_client
        self._config = config
        self._current_instrument = config.chart_default_instrument
        self._show_sma = True
        self._show_ema = True
        self._candle_weight: float = 0.8   # default: 1-minute fill
        self._build(tab_bar)

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self, tab_bar: int | str) -> None:
        with dpg.tab(label="  Charts  ", parent=tab_bar, tag="tab_charts"):
            dpg.add_spacer(height=6)

            # ── Toolbar ───────────────────────────────────────────────────────
            with dpg.group(horizontal=True):
                dpg.add_text("Instrument:", color=MUTED)
                dpg.add_input_text(
                    tag="chart_symbol_input",
                    default_value=self._current_instrument,
                    width=140,
                )
                dpg.add_button(
                    label="Subscribe", width=90,
                    callback=self._on_subscribe,
                )
                dpg.add_spacer(width=20)
                dpg.add_text("Timeframe:", color=MUTED)
                dpg.add_combo(
                    ["1 Minute", "5 Minutes", "15 Minutes", "1 Hour"],
                    tag="chart_tf_combo",
                    default_value="1 Minute",
                    width=110,
                )
                dpg.add_spacer(width=20)
                dpg.add_checkbox(
                    label=f"SMA {self._config.sma_period}",
                    tag="chart_sma_cb",
                    default_value=True,
                    callback=lambda s, v, u: setattr(self, "_show_sma", v),
                )
                dpg.add_spacer(width=8)
                dpg.add_checkbox(
                    label=f"EMA {self._config.ema_period}",
                    tag="chart_ema_cb",
                    default_value=True,
                    callback=lambda s, v, u: setattr(self, "_show_ema", v),
                )
                dpg.add_spacer(width=20)
                dpg.add_text("Width:", color=MUTED)
                dpg.add_input_float(
                    tag="chart_weight_input",
                    default_value=self._candle_weight,
                    width=70,
                    step=0.0,
                    format="%.2f",
                    callback=self._on_weight_changed,
                )

            dpg.add_spacer(height=6)

            # ── Plot ──────────────────────────────────────────────────────────
            with dpg.plot(
                tag="main_chart",
                label="",
                height=-1,
                width=-1,
                use_local_time=True,
            ):
                dpg.add_plot_legend()

                dpg.add_plot_axis(
                    dpg.mvXAxis,
                    label="",
                    time=True,
                    tag="chart_x_axis",
                )
                with dpg.plot_axis(
                    dpg.mvYAxis, label="Price", tag="chart_y_axis"
                ):
                    # Candle series
                    dpg.add_candle_series(
                        dates=[],
                        opens=[],
                        closes=[],
                        lows=[],
                        highs=[],
                        label="OHLC",
                        tag="candle_series",
                        bull_color=(30, 200, 90, 255),
                        bear_color=(200, 50, 50, 255),
                        weight=self._candle_weight,
                        time_unit=dpg.mvTimeUnit_Min,
                    )
                    # Indicator overlays
                    dpg.add_line_series([], [], label=f"SMA {self._config.sma_period}", tag="sma_series")
                    dpg.add_line_series([], [], label=f"EMA {self._config.ema_period}", tag="ema_series")

            # Theme for indicator lines
            with dpg.theme() as sma_theme:
                with dpg.theme_component(dpg.mvLineSeries):
                    dpg.add_theme_color(dpg.mvPlotCol_Line, (50, 180, 250, 220), category=dpg.mvThemeCat_Plots)
            dpg.bind_item_theme("sma_series", sma_theme)

            with dpg.theme() as ema_theme:
                with dpg.theme_component(dpg.mvLineSeries):
                    dpg.add_theme_color(dpg.mvPlotCol_Line, (250, 180, 50, 220), category=dpg.mvThemeCat_Plots)
            dpg.bind_item_theme("ema_series", ema_theme)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _on_weight_changed(self, sender, app_data, user_data) -> None:
        self._candle_weight = max(0.01, float(app_data))
        dpg.configure_item("candle_series", weight=self._candle_weight)

    def _on_subscribe(self, sender, app_data, user_data) -> None:
        instrument = dpg.get_value("chart_symbol_input").strip()
        tf_str     = dpg.get_value("chart_tf_combo")
        period     = self._TF_PERIOD.get(tf_str, 1)

        if instrument:
            self._current_instrument = instrument

            # Recalculate weight so candles fill ~80 % of each bar slot.
            # weight × 60 s (mvTimeUnit_Min) = 0.8 × (period × 60 s)
            # → weight = 0.8 × period
            self._candle_weight = round(0.8 * period, 4)
            dpg.set_value("chart_weight_input", self._candle_weight)
            dpg.configure_item("candle_series", weight=self._candle_weight)

            self._nt.subscribe_bars(instrument, "Minute", period)
            self._nt.subscribe_market_data(instrument)
            logger.info("Subscribed to bars: %s (%s) weight=%.2f",
                        instrument, tf_str, self._candle_weight)

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _resolve_instrument(self) -> str:
        """
        Return the key that actually exists in the candles dict for the current
        display instrument, handling the common case where NinjaTrader sends the
        root symbol ("ES") while the user typed a full name ("ES 03-25").
        Priority: exact → root-word match → any dirty key → first available.
        """
        target = self._current_instrument.strip()
        with self._state._lock:
            keys        = list(self._state.candles.keys())
            dirty_keys  = [k for k, v in self._state.candles_dirty.items() if v]

        if not keys:
            return target

        target_up = target.upper()
        target_root = target_up.split()[0]

        # 1 – Exact (case-insensitive)
        for k in keys:
            if k.strip().upper() == target_up:
                return k

        # 2 – Root-word match: "ES" ↔ "ES 03-25"
        for k in keys:
            if k.strip().upper().split()[0] == target_root:
                if self._current_instrument != k:
                    self._current_instrument = k
                    dpg.set_value("chart_symbol_input", k)
                return k

        # 3 – Any instrument that already has new (dirty) data
        if dirty_keys:
            best = dirty_keys[0]
            self._current_instrument = best
            dpg.set_value("chart_symbol_input", best)
            return best

        # 4 – Fall back to whatever instrument has candles
        return keys[0]

    def refresh(self) -> None:
        instrument = self._resolve_instrument()
        if not self._state.candles_check_and_clear(instrument):
            dpg.configure_item("sma_series", show=self._show_sma)
            dpg.configure_item("ema_series", show=self._show_ema)
            return

        candles = self._state.get_candles(instrument)
        if not candles:
            return

        dates  = [c.timestamp for c in candles]
        opens  = [c.open      for c in candles]
        highs  = [c.high      for c in candles]
        lows   = [c.low       for c in candles]
        closes = [c.close     for c in candles]

        dpg.set_value("candle_series", [dates, opens, closes, lows, highs])

        # SMA overlay
        if self._show_sma:
            sma_vals = _sma(closes, self._config.sma_period)
            valid = [(d, v) for d, v in zip(dates, sma_vals) if v == v]  # skip nan
            if valid:
                dpg.set_value("sma_series", [[d for d, _ in valid], [v for _, v in valid]])
        dpg.configure_item("sma_series", show=self._show_sma)

        # EMA overlay
        if self._show_ema:
            ema_vals = _ema(closes, self._config.ema_period)
            valid = [(d, v) for d, v in zip(dates, ema_vals) if v == v]
            if valid:
                dpg.set_value("ema_series", [[d for d, _ in valid], [v for _, v in valid]])
        dpg.configure_item("ema_series", show=self._show_ema)

        # Auto-fit axes
        dpg.fit_axis_data("chart_x_axis")
        dpg.fit_axis_data("chart_y_axis")
