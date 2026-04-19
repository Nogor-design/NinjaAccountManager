"""Central application configuration."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AppConfig:
    # ── WebSocket server ──────────────────────────────────────────────────────
    host: str = "127.0.0.1"
    port: int = 8765

    # ── Logging ───────────────────────────────────────────────────────────────
    log_dir: Path = field(default_factory=lambda: Path("logs"))
    log_level: str = "INFO"
    log_max_bytes: int = 10 * 1024 * 1024  # 10 MB
    log_backup_count: int = 5

    # ── Chart ─────────────────────────────────────────────────────────────────
    chart_max_candles: int = 200
    chart_default_instrument: str = "ES 03-25"

    # ── Indicators ────────────────────────────────────────────────────────────
    sma_period: int = 20
    ema_period: int = 9

    # ── GUI ───────────────────────────────────────────────────────────────────
    window_width: int = 1440
    window_height: int = 900
    gui_log_max_lines: int = 500
