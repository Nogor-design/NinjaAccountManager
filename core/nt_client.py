"""
NinjaTraderClient
=================
Runs a WebSocket **server** in a dedicated asyncio loop on a background thread.
NinjaTrader 8 (with the NinjaAccountManager NinjaScript installed) connects as
a WebSocket **client** and streams JSON messages.

Message protocol
----------------
FROM NinjaTrader → Python (incoming):
  {"type": "ACCOUNT",     "data": { ... AccountData fields ... }}
  {"type": "POSITION",    "data": { ... Position fields ... }}
  {"type": "ORDER",       "data": { ... Order fields ... }}
  {"type": "MARKET_DATA", "data": { ... MarketData fields ... }}
  {"type": "BAR",         "data": { ... Candle fields ... }}

FROM Python → NinjaTrader (outgoing commands):
  {"action": "SUBMIT_ORDER",  "account": ..., "instrument": ...,
   "orderAction": "Buy"|"Sell", "orderType": "Market"|"Limit"|"Stop"|"StopLimit",
   "quantity": N, "price": 0.0, "stopPrice": 0.0}
  {"action": "CANCEL_ORDER",  "orderId": "..."}
  {"action": "SUBSCRIBE_MD",  "instrument": "ES 03-25"}
  {"action": "SUBSCRIBE_BARS","instrument": "ES 03-25", "barType": "Minute", "period": 1}
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import threading
from typing import Any

import websockets
from websockets.server import WebSocketServerProtocol

from core.config import AppConfig
from core.data_models import AccountData, Position, Order, MarketData, Candle
from core.event_bus import EventBus, Events

logger = logging.getLogger(__name__)

# Fields that exist on each dataclass (pre-computed for fast filtering)
_ACCOUNT_FIELDS = {f.name for f in dataclasses.fields(AccountData)}
_POSITION_FIELDS = {f.name for f in dataclasses.fields(Position)}
_ORDER_FIELDS = {f.name for f in dataclasses.fields(Order)}
_MD_FIELDS = {f.name for f in dataclasses.fields(MarketData)}
_CANDLE_FIELDS = {f.name for f in dataclasses.fields(Candle)}


def _safe_build(cls, raw: dict) -> Any:
    """Construct a dataclass from a raw dict, ignoring unknown keys."""
    fields = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in fields})


class NinjaTraderClient:
    """
    WebSocket server that NinjaTrader 8 connects to.

    Usage::

        client = NinjaTraderClient(config, event_bus)
        client.start()   # non-blocking; spins up background thread
        ...
        client.stop()
    """

    def __init__(self, config: AppConfig, event_bus: EventBus) -> None:
        self._config = config
        self._bus = event_bus
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._connections: dict[str, WebSocketServerProtocol] = {}
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the WebSocket server on a daemon thread (non-blocking)."""
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="NTSocketThread"
        )
        self._thread.start()
        logger.info("NinjaTraderClient started; waiting for NT8 connections.")

    def stop(self) -> None:
        """Signal the server to stop and wait for the thread to finish."""
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    # ── Internal asyncio plumbing ─────────────────────────────────────────────

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except RuntimeError as exc:
            # "Event loop stopped before Future completed" — raised when stop()
            # is called from outside (e.g. on app shutdown). Not a real crash.
            if "Event loop stopped" not in str(exc):
                logger.exception("WebSocket server crashed.")
        except Exception:  # noqa: BLE001
            logger.exception("WebSocket server crashed.")

    async def _serve(self) -> None:
        host, port = self._config.host, self._config.port
        async with websockets.serve(self._handle_connection, host, port):
            logger.info("WebSocket server listening on ws://%s:%s", host, port)
            self._bus.publish(
                Events.CONNECTION_STATUS,
                {"status": "listening", "host": host, "port": port},
            )
            while self._running:
                await asyncio.sleep(0.5)

    async def _handle_connection(
        self, websocket: WebSocketServerProtocol, path: str = "/"
    ) -> None:
        addr = str(websocket.remote_address)
        self._connections[addr] = websocket
        logger.info("NinjaTrader connected from %s", addr)
        self._bus.publish(Events.CONNECTED, {"client_id": addr})
        try:
            async for raw in websocket:
                await self._handle_message(str(raw))
        except websockets.ConnectionClosed:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("Error in connection handler for %s", addr)
        finally:
            self._connections.pop(addr, None)
            logger.info("NinjaTrader disconnected: %s", addr)
            self._bus.publish(Events.DISCONNECTED, {"client_id": addr})

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Received invalid JSON (first 120 chars): %s", raw[:120])
            return

        msg_type: str = msg.get("type", "")
        data: dict = msg.get("data", {})

        try:
            if msg_type == "ACCOUNT":
                self._bus.publish(Events.ACCOUNT_UPDATE, _safe_build(AccountData, data))
            elif msg_type == "POSITION":
                self._bus.publish(Events.POSITION_UPDATE, _safe_build(Position, data))
            elif msg_type == "ORDER":
                self._bus.publish(Events.ORDER_UPDATE, _safe_build(Order, data))
            elif msg_type == "MARKET_DATA":
                self._bus.publish(Events.MARKET_DATA, _safe_build(MarketData, data))
            elif msg_type == "BAR":
                self._bus.publish(Events.BAR_UPDATE, _safe_build(Candle, data))
            else:
                logger.debug("Unknown message type: %r", msg_type)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to process message type %r", msg_type)

    # ── Command senders (called from GUI thread) ──────────────────────────────

    def _send_all(self, payload: dict) -> None:
        """Broadcast a JSON command to every connected NinjaTrader instance."""
        if not self._connections or self._loop is None:
            logger.warning("No NinjaTrader connections; command dropped.")
            return
        text = json.dumps(payload)
        for ws in list(self._connections.values()):
            asyncio.run_coroutine_threadsafe(ws.send(text), self._loop)

    def submit_order(
        self,
        account: str,
        instrument: str,
        action: str,
        order_type: str,
        quantity: int,
        price: float = 0.0,
        stop_price: float = 0.0,
    ) -> None:
        self._send_all(
            {
                "action": "SUBMIT_ORDER",
                "account": account,
                "instrument": instrument,
                "orderAction": action,
                "orderType": order_type,
                "quantity": quantity,
                "price": price,
                "stopPrice": stop_price,
            }
        )
        logger.info(
            "Order submitted: %s %s %s x%d @ %.2f", action, order_type, instrument, quantity, price
        )

    def cancel_order(self, order_id: str) -> None:
        self._send_all({"action": "CANCEL_ORDER", "orderId": order_id})
        logger.info("Cancel order: %s", order_id)

    def subscribe_market_data(self, instrument: str) -> None:
        self._send_all({"action": "SUBSCRIBE_MD", "instrument": instrument})

    def subscribe_bars(
        self, instrument: str, bar_type: str = "Minute", period: int = 1
    ) -> None:
        self._send_all(
            {
                "action": "SUBSCRIBE_BARS",
                "instrument": instrument,
                "barType": bar_type,
                "period": period,
            }
        )

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return len(self._connections) > 0

    @property
    def connection_count(self) -> int:
        return len(self._connections)
