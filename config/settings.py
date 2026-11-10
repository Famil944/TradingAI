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
        default_factory=lambda: max(1, int(os.getenv("OTC_MAX_INSTRUMENTS", "3")))
    )
    cooldown_seconds: int = field(
        default_factory=lambda: max(60, int(os.getenv("OTC_COOLDOWN_SECONDS", "900")))
    )
    max_tick_age: float = 8.0
    max_tick_gap: float = 8.0
    payout_max_age: float = 60.0
    settlement_tolerance: float = 2.0


settings = Settings()
