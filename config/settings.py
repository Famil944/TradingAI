"""Local configuration; secrets are never serialized or logged."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    mini_app_enabled: bool = field(
        default_factory=lambda: os.getenv("OTC_MINI_APP_ENABLED", "false").lower()
        in ("1", "true", "yes", "on")
    )
    mini_app_url: str = field(
        default_factory=lambda: os.getenv("OTC_MINI_APP_URL", "").strip()
    )
    mini_app_port: int = field(
        default_factory=lambda: int(os.getenv("OTC_MINI_APP_PORT", "8091"))
    )
    primary_mode: str = field(
        default_factory=lambda: os.getenv("OTC_PRIMARY_MODE", "conditional")
    )
    groq_api_key: str = field(
        default_factory=lambda: os.getenv("GROQ_API_KEY", ""), repr=False
    )
    groq_model: str = field(
        default_factory=lambda: os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
    )
    groq_interval_seconds: int = field(
        default_factory=lambda: max(60, int(os.getenv("GROQ_INTERVAL_SECONDS", "300")))
    )
    groq_max_requests: int = field(
        default_factory=lambda: max(0, int(os.getenv("GROQ_MAX_REQUESTS", "100")))
    )
    groq_max_tokens: int = field(
        default_factory=lambda: max(0, int(os.getenv("GROQ_MAX_TOKENS", "150000")))
    )
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
    catalogue_refresh_seconds: int = field(
        default_factory=lambda: max(
            15, min(55, int(os.getenv("OTC_CATALOGUE_REFRESH_SECONDS", "50")))
        )
    )
    m1_strict_enabled: bool = field(
        default_factory=lambda: os.getenv("OTC_M1_STRICT_ENABLED", "true").lower()
        in ("1", "true", "yes", "on")
    )
    primary_policy_enforced: bool = field(
        default_factory=lambda: os.getenv(
            "OTC_PRIMARY_POLICY_ENFORCED", "false"
        ).lower() in ("1", "true", "yes", "on")
    )
    ready_min_confirmations: int = field(
        default_factory=lambda: int(os.getenv("OTC_READY_MIN_CONFIRMATIONS", "3"))
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
    entry_quality_recheck: bool = True
    compact_messages: bool = True
    require_paid_metadata: bool = True
    snapshot_max_age: float = 30.0
    max_tick_age: float = 8.0
    max_tick_gap: float = 8.0
    stream_stale_timeout: float = 15.0
    payout_max_age: float = 60.0
    settlement_tolerance: float = 2.0

    def __post_init__(self):
        if not 1 <= self.mini_app_port <= 65535:
            raise ValueError("OTC_MINI_APP_PORT must be 1..65535")
        if self.mini_app_url:
            from urllib.parse import urlsplit
            url = urlsplit(self.mini_app_url)
            if (url.scheme != "https" or not url.hostname
                    or url.username or url.password or url.fragment):
                raise ValueError("OTC_MINI_APP_URL must be a public HTTPS URL")
        if self.primary_mode not in ("conditional", "ready"):
            raise ValueError("OTC_PRIMARY_MODE must be conditional or ready")
        if self.signal_mode not in ("immediate", "conditional"):
            raise ValueError("OTC_SIGNAL_MODE must be immediate or conditional")
        if not 2 <= self.ready_min_confirmations <= 5:
            raise ValueError("OTC_READY_MIN_CONFIRMATIONS must be 2..5")


settings = Settings()
