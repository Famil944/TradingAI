"""Local configuration; secrets are never serialized or logged."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""), repr=False
    )
    otcharts_api_key: str = field(
        default_factory=lambda: os.getenv("OTCHARTS_API_KEY", ""), repr=False
    )
    admin_chat_id: int = field(
        default_factory=lambda: int(os.getenv("OTC_ADMIN_CHAT_ID", "0") or "0")
    )
    database_path: str = field(
        default_factory=lambda: (
            os.getenv("TRADING_AI_DB_PATH", "")
            or os.getenv("DATABASE_URL", "data/bot.db")
            .removeprefix("sqlite+aiosqlite:///")
            .removeprefix("sqlite:///")
        )
    )
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    max_instruments: int = field(
        default_factory=lambda: max(1, int(os.getenv("OTC_MAX_INSTRUMENTS", "5")))
    )
    cooldown_seconds: int = field(
        default_factory=lambda: max(60, int(os.getenv("OTC_COOLDOWN_SECONDS", "120")))
    )
    test_mode: bool = field(
        default_factory=lambda: (
            os.getenv("OTC_TEST_MODE", "false").lower() in ("1", "true", "yes", "on")
        )
    )
    poll_seconds: int = field(
        default_factory=lambda: max(60, int(os.getenv("OTC_POLL_SECONDS", "300")))
    )
    poll_request_budget: int = field(
        default_factory=lambda: max(2, int(os.getenv("OTC_POLL_REQUEST_BUDGET", "12")))
    )
    health_report_seconds: int = field(
        default_factory=lambda: max(
            0, int(os.getenv("OTC_HEALTH_REPORT_SECONDS", "120"))
        )
    )
    signal_mode: str = field(
        default_factory=lambda: (
            os.getenv("OTC_SIGNAL_MODE", "immediate").strip().lower()
        )
    )
    snapshot_max_age: float = 30.0
    max_tick_age: float = 8.0
    max_tick_gap: float = 8.0
    stream_stale_timeout: float = 15.0
    payout_max_age: float = 60.0
    settlement_tolerance: float = 2.0

    def __post_init__(self):
        if self.signal_mode not in ("immediate", "conditional"):
            raise ValueError("OTC_SIGNAL_MODE must be immediate or conditional")


settings = Settings()
