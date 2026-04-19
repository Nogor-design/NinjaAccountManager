"""
NinjaApp
========
Main DearPyGUI application class.

Architecture
------------
* DearPyGUI's render loop runs on the **main thread**.
* We use the manual render loop (``while dpg.is_dearpygui_running()``) so we
  can call each panel's ``refresh()`` method once per frame without needing
  DearPyGUI callbacks or timers.
* Background threads (WebSocket server) write to ``AppState`` via thread-safe
  mutators; the main thread reads state and updates widgets each frame.
"""
from __future__ import annotations
import logging

import dearpygui.dearpygui as dpg

from core.config import AppConfig
from core.nt_client import NinjaTraderClient
from core.state import AppState
from gui.dashboard import DashboardPanel
from gui.accounts import AccountsPanel
from gui.positions import PositionsPanel
from gui.orders import OrdersPanel
from gui.charts import ChartsPanel
from gui.logs import LogsPanel

logger = logging.getLogger(__name__)


class NinjaApp:
    def __init__(
        self,
        config: AppConfig,
        state: AppState,
        nt_client: NinjaTraderClient,
    ) -> None:
        self._config = config
        self._state = state
        self._nt = nt_client
        self._panels: list = []

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self) -> None:
        dpg.create_context()
        self._build_theme()
        self._build_ui()
        dpg.create_viewport(
            title="NinjaAccountManager – NinjaTrader 8 Desktop Monitor",
            width=self._config.window_width,
            height=self._config.window_height,
            small_icon="",
            large_icon="",
        )
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("primary_window", True)

        # Manual render loop – lets us drive panel refreshes each frame
        while dpg.is_dearpygui_running():
            for panel in self._panels:
                try:
                    panel.refresh()
                except Exception:  # noqa: BLE001
                    logger.exception("Panel refresh error.")
            dpg.render_dearpygui_frame()

        dpg.destroy_context()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        with dpg.window(tag="primary_window", no_title_bar=True, no_move=True,
                        no_resize=True, no_close=True):
            self._build_header()
            with dpg.tab_bar(tag="main_tabs"):
                panels = [
                    DashboardPanel("main_tabs", self._state),
                    AccountsPanel("main_tabs", self._state),
                    PositionsPanel("main_tabs", self._state),
                    OrdersPanel("main_tabs", self._state, self._nt),
                    ChartsPanel("main_tabs", self._state, self._nt, self._config),
                    LogsPanel("main_tabs", self._state),
                ]
                self._panels = panels

        # Bind the dark plot theme now that "main_chart" exists
        try:
            dpg.bind_item_theme("main_chart", self._plot_theme)
        except Exception:  # noqa: BLE001
            pass

    def _build_header(self) -> None:
        with dpg.group(horizontal=True):
            dpg.add_text(
                "NinjaAccountManager",
                color=(120, 190, 255, 255),
            )
            dpg.add_spacer(width=16)
            dpg.add_text("●", tag="hdr_status_dot", color=(220, 60, 60, 255))
            dpg.add_text("", tag="hdr_status_label", color=(180, 180, 200, 255))
            dpg.add_spacer(width=10)
            dpg.add_text(
                f"ws://{self._config.host}:{self._config.port}",
                color=(100, 130, 160, 255),
            )
        dpg.add_separator()
        dpg.add_spacer(height=2)

        # Header connection dot is updated each frame inside DashboardPanel.refresh()

    # ── Theme ─────────────────────────────────────────────────────────────────

    def _build_theme(self) -> None:
        with dpg.theme() as global_theme:
            with dpg.theme_component(dpg.mvAll):
                # Backgrounds
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg,       (12, 12, 18, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg,        (18, 18, 26, 255))
                dpg.add_theme_color(dpg.mvThemeCol_PopupBg,        (22, 22, 32, 255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg,        (28, 28, 40, 255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (38, 38, 55, 255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive,  (48, 48, 68, 255))
                # Border
                dpg.add_theme_color(dpg.mvThemeCol_Border,         (50, 50, 68, 255))
                dpg.add_theme_color(dpg.mvThemeCol_BorderShadow,   (0, 0, 0, 0))
                # Title
                dpg.add_theme_color(dpg.mvThemeCol_TitleBg,        (12, 12, 18, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive,  (18, 90, 160, 255))
                # Tabs
                dpg.add_theme_color(dpg.mvThemeCol_Tab,            (22, 22, 32, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TabHovered,     (35, 105, 185, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TabActive,      (25, 95, 170, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TabUnfocused,   (18, 18, 26, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TabUnfocusedActive, (22, 70, 130, 255))
                # Buttons
                dpg.add_theme_color(dpg.mvThemeCol_Button,         (28, 75, 140, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,  (38, 95, 170, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,   (18, 55, 110, 255))
                # Text
                dpg.add_theme_color(dpg.mvThemeCol_Text,           (215, 215, 230, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TextDisabled,   (110, 110, 130, 255))
                # Table
                dpg.add_theme_color(dpg.mvThemeCol_TableRowBg,     (18, 18, 26, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TableRowBgAlt,  (24, 24, 34, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight,(40, 40, 56, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TableBorderStrong,(55, 55, 75, 255))
                # Headers (table column headers)
                dpg.add_theme_color(dpg.mvThemeCol_Header,         (28, 80, 150, 180))
                dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered,  (38, 100, 180, 200))
                dpg.add_theme_color(dpg.mvThemeCol_HeaderActive,   (20, 60, 120, 255))
                # Scrollbar
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg,    (12, 12, 18, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab,  (45, 45, 65, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabHovered, (60, 60, 85, 255))
                # Separators / lines
                dpg.add_theme_color(dpg.mvThemeCol_Separator,      (50, 50, 70, 255))
                dpg.add_theme_color(dpg.mvThemeCol_CheckMark,      (80, 200, 120, 255))
                # Rounding & spacing
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding,   4)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding,    4)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding,    4)
                dpg.add_theme_style(dpg.mvStyleVar_TabRounding,      4)
                dpg.add_theme_style(dpg.mvStyleVar_GrabRounding,     4)
                dpg.add_theme_style(dpg.mvStyleVar_PopupRounding,    4)
                dpg.add_theme_style(dpg.mvStyleVar_ScrollbarRounding, 4)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing,      6, 4)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding,     6, 4)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding,    8, 8)
                dpg.add_theme_style(dpg.mvStyleVar_CellPadding,      4, 3)

        dpg.bind_theme(global_theme)

        # Plot theme (dark background, grid lines)
        with dpg.theme() as plot_theme:
            with dpg.theme_component(dpg.mvPlot):
                dpg.add_theme_color(dpg.mvPlotCol_FrameBg,    (18, 18, 26, 255), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_PlotBg,     (12, 12, 18, 255), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_PlotBorder, (50, 50, 70, 255), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_LegendBg,   (22, 22, 32, 220), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_LegendBorder,(50, 50, 70, 200), category=dpg.mvThemeCat_Plots)
                # DPG 2.x uses unified axis color constants (no separate X/Y)
                dpg.add_theme_color(dpg.mvPlotCol_AxisText, (180, 180, 200, 255), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_AxisGrid, (45,  45,  65, 160), category=dpg.mvThemeCat_Plots)
        # Apply plot theme globally
        # Store for deferred binding after the chart widget is created
        self._plot_theme = plot_theme
