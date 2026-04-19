"""
NinjaAccountManager – entry point
==================================
Boots the WebSocket server and the DearPyGUI desktop application.

Run with:
    python main.py
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from core.config import AppConfig
from core.event_bus import EventBus, Events
from core.state import AppState
from core.nt_client import NinjaTraderClient
from gui.app import NinjaApp


# ── Logging setup ─────────────────────────────────────────────────────────────

def _configure_logging(config: AppConfig) -> None:
    config.log_dir.mkdir(exist_ok=True)
    log_file = config.log_dir / "ninja_account_manager.log"

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.log_level, logging.INFO))

    # Rotating file handler
    fh = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    config = AppConfig()
    _configure_logging(config)
    logger = logging.getLogger(__name__)
    logger.info("NinjaAccountManager starting (host=%s port=%d)", config.host, config.port)

    event_bus = EventBus()
    state = AppState(
        max_log_lines=config.gui_log_max_lines,
        max_candles=config.chart_max_candles,
    )

    # ── Wire event bus → AppState ─────────────────────────────────────────────
    # Track how many NinjaTrader clients are connected
    _connected: list[int] = [0]

    def on_connected(data: dict) -> None:
        _connected[0] += 1
        state.nt_connected = True
        state.add_log(f"[CONNECTION] NinjaTrader connected: {data.get('client_id', '')}")

    def on_disconnected(data: dict) -> None:
        _connected[0] = max(0, _connected[0] - 1)
        state.nt_connected = _connected[0] > 0
        state.add_log("[CONNECTION] NinjaTrader disconnected.")
        if not state.nt_connected:
            state.clear_trading_data()
            state.add_log("[CONNECTION] Trading data cleared — awaiting reconnect.")

    def on_connection_status(data: dict) -> None:
        state.add_log(f"[SERVER] WebSocket server {data.get('status', '')} "
                      f"on {data.get('host', '')}:{data.get('port', '')}")

    event_bus.subscribe(Events.CONNECTED,          on_connected)
    event_bus.subscribe(Events.DISCONNECTED,       on_disconnected)
    event_bus.subscribe(Events.CONNECTION_STATUS,  on_connection_status)

    event_bus.subscribe(Events.ACCOUNT_UPDATE, lambda d: (
        state.update_account(d),
        state.add_log(f"[ACCOUNT]  {d.name}  balance=${d.balance:,.2f}  "
                      f"rPnL=${d.realized_pnl:,.2f}  uPnL=${d.unrealized_pnl:,.2f}"),
    ))

    event_bus.subscribe(Events.POSITION_UPDATE, lambda d: (
        state.update_position(d),
        state.add_log(
            f"[POSITION] {d.instrument}  qty={d.quantity}  "
            f"avg={d.avg_price:.2f}  uPnL=${d.unrealized_pnl:,.2f}"
        ) if d.quantity != 0 else
        state.add_log(f"[POSITION] {d.instrument} closed"),
    ))

    event_bus.subscribe(Events.ORDER_UPDATE, lambda d: (
        state.update_order(d),
        state.add_log(
            f"[ORDER]    {d.order_id[:8]}  {d.action} {d.order_type} "
            f"{d.instrument} x{d.quantity}  status={d.status}"
        ),
    ))

    event_bus.subscribe(Events.MARKET_DATA, state.update_market_data)
    event_bus.subscribe(Events.BAR_UPDATE,  state.add_candle)

    event_bus.subscribe(Events.LOG_MESSAGE, state.add_log)

    # Mirror Python logger → GUI log panel
    class GUILogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            level_tag = f"[{record.levelname}]"
            state.add_log(f"{level_tag} {record.name}: {record.getMessage()}")

    gui_handler = GUILogHandler()
    gui_handler.setLevel(logging.WARNING)   # only warnings+ in GUI
    logging.getLogger().addHandler(gui_handler)

    # ── Start WebSocket server ────────────────────────────────────────────────
    nt_client = NinjaTraderClient(config=config, event_bus=event_bus)
    nt_client.start()
    state.is_server_running = True
    state.add_log(f"[SERVER] Listening for NinjaTrader on ws://{config.host}:{config.port}")

    # ── Launch GUI (blocks until window is closed) ────────────────────────────
    try:
        app = NinjaApp(config=config, state=state, nt_client=nt_client)
        app.run()
    finally:
        logger.info("Shutting down WebSocket server.")
        nt_client.stop()
        logger.info("NinjaAccountManager stopped.")


if __name__ == "__main__":
    main()
