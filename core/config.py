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
    gui_strategy_event_max_lines: int = 300

    # Strategy execution guardrails
    execution_mode: str = "SIM"
    strategy_allowed_accounts: tuple[str, ...] = ("Sim101",)
    strategy_require_single_nt_connection: bool = True
    strategy_test_heartbeat_seconds: int = 20
    strategy_test_default_stop_ticks: int = 80
    strategy_test_default_target_ticks: int = 0
    strategy_test_bracket_target_ticks: int = 40
    strategy_test_report_dir: Path = field(default_factory=lambda: Path("logs") / "strategy_smoke_reports")
    strategy_cleanup_retry_seconds: float = 5.0
    strategy_cleanup_timeout_seconds: float = 45.0

    # Direct strategy runtime API
    strategy_api_enabled: bool = True
    strategy_api_host: str = "127.0.0.1"
    strategy_api_port: int = 8766

    # Legacy file bridge fallback
    legacy_file_bridge_enabled: bool = False
    bridge_root: Path = field(default_factory=lambda: Path("bridge"))
    bridge_poll_interval_seconds: float = 0.5
    bridge_state_flush_seconds: float = 2.0
    bridge_default_account: str = "Sim101"
    bridge_tick_size: float = 0.25
    bridge_stale_signal_seconds: int = 60
    bridge_future_skew_seconds: int = 10
    bridge_heartbeat_timeout_seconds: int = 60
    bridge_max_position_size: int = 3
    bridge_max_stop_ticks_cap: int = 200
    bridge_require_instrument_match: bool = False
    bridge_processed_ids_retain_count: int = 200
