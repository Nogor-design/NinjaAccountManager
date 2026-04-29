from __future__ import annotations

import json
import logging
import socketserver
import threading
from typing import Any

from core.config import AppConfig
from core.event_bus import EventBus, Events
from core.strategy_bridge import StrategyBridgeService

logger = logging.getLogger(__name__)


class _StrategyTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_class, api: "StrategyAPIServer") -> None:
        super().__init__(server_address, handler_class)
        self.api = api


class _StrategyRequestHandler(socketserver.StreamRequestHandler):
    def setup(self) -> None:
        super().setup()
        self._send_lock = threading.Lock()
        self.server.api.register_client(self)  # type: ignore[attr-defined]

    def finish(self) -> None:
        try:
            self.server.api.unregister_client(self)  # type: ignore[attr-defined]
        finally:
            super().finish()

    def handle(self) -> None:
        self.server.api.send_snapshot(self)  # type: ignore[attr-defined]
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self.server.api.send_event(  # type: ignore[attr-defined]
                    self,
                    {
                        "message_type": "EVENT",
                        "event": "ERROR",
                        "signal_id": "",
                        "correlation_id": "",
                        "timestamp": "",
                        "account": "",
                        "instrument": "",
                        "runtime_state": "",
                        "position_id": "",
                        "source": "strategy_api",
                        "error_code": "invalid_json",
                        "details": {"message": "invalid json"},
                    },
                )
                continue
            self.server.api.submit(payload, self.client_address)  # type: ignore[attr-defined]

    def send_json(self, payload: dict[str, Any]) -> None:
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        with self._send_lock:
            self.wfile.write(data)
            self.wfile.flush()


class StrategyAPIServer:
    def __init__(self, config: AppConfig, event_bus: EventBus, runtime: StrategyBridgeService) -> None:
        self._config = config
        self._bus = event_bus
        self._runtime = runtime
        self._lock = threading.RLock()
        self._clients: set[_StrategyRequestHandler] = set()
        self._server: _StrategyTCPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._bus.subscribe(Events.STRATEGY_EVENT, self._broadcast_event)
        self._server = _StrategyTCPServer(
            (self._config.strategy_api_host, self._config.strategy_api_port),
            _StrategyRequestHandler,
            self,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="StrategyAPIServer",
        )
        self._thread.start()
        logger.info(
            "Strategy API listening on tcp://%s:%s",
            self._config.strategy_api_host,
            self._config.strategy_api_port,
        )
        self._publish_client_status(listening=True)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._publish_client_status(listening=False)

    def register_client(self, handler: _StrategyRequestHandler) -> None:
        with self._lock:
            self._clients.add(handler)
        self._publish_client_status(listening=True)

    def unregister_client(self, handler: _StrategyRequestHandler) -> None:
        with self._lock:
            self._clients.discard(handler)
        self._publish_client_status(listening=self._server is not None)

    def send_snapshot(self, handler: _StrategyRequestHandler) -> None:
        handler.send_json(self._runtime.build_state_snapshot(reason="client_connected"))

    def send_event(self, handler: _StrategyRequestHandler, event: dict[str, Any]) -> None:
        try:
            handler.send_json(event)
        except Exception:  # noqa: BLE001
            self.unregister_client(handler)

    def submit(self, payload: dict[str, Any], client_address: Any) -> None:
        payload = dict(payload)
        payload.setdefault("source", f"{client_address[0]}:{client_address[1]}")
        self._runtime.submit_instruction_payload(payload, source="strategy_api")

    def _broadcast_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            clients = list(self._clients)
        for handler in clients:
            try:
                handler.send_json(event)
            except Exception:  # noqa: BLE001
                self.unregister_client(handler)

    def _publish_client_status(self, *, listening: bool) -> None:
        self._bus.publish(
            Events.STRATEGY_CLIENT_STATUS,
            {
                "client_count": self.connection_count,
                "listening": listening,
                "host": self._config.strategy_api_host,
                "port": self._config.strategy_api_port,
                "endpoint": f"tcp://{self._config.strategy_api_host}:{self._config.strategy_api_port}",
            },
        )

    @property
    def connection_count(self) -> int:
        with self._lock:
            return len(self._clients)
