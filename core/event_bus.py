"""Thread-safe publish/subscribe event bus."""
from __future__ import annotations
import threading
from collections import defaultdict
from typing import Any, Callable


class EventBus:
    """
    Lightweight pub/sub bus used to decouple the WebSocket layer from the GUI.
    All public methods are safe to call from any thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[Callable[[Any], None]]] = defaultdict(list)

    def subscribe(self, event_type: str, callback: Callable[[Any], None]) -> None:
        with self._lock:
            self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Callable[[Any], None]) -> None:
        with self._lock:
            try:
                self._subscribers[event_type].remove(callback)
            except ValueError:
                pass

    def publish(self, event_type: str, data: Any = None) -> None:
        with self._lock:
            callbacks = list(self._subscribers[event_type])
        for cb in callbacks:
            try:
                cb(data)
            except Exception as exc:  # noqa: BLE001
                # Prevent a bad subscriber from breaking the entire bus.
                import logging
                logging.getLogger(__name__).exception(
                    "EventBus subscriber error (event=%s): %s", event_type, exc
                )


class Events:
    """String constants for all event types."""
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    CONNECTION_STATUS = "CONNECTION_STATUS"
    ACCOUNT_UPDATE = "ACCOUNT_UPDATE"
    POSITION_UPDATE = "POSITION_UPDATE"
    ORDER_UPDATE = "ORDER_UPDATE"
    MARKET_DATA = "MARKET_DATA"
    BAR_UPDATE = "BAR_UPDATE"
    LOG_MESSAGE = "LOG_MESSAGE"
