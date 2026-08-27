from pydantic import BaseModel
from pathlib import Path
from typing import Optional
import os
from dotenv import load_dotenv

# Загружаем .env файл
load_dotenv()


class Settings(BaseModel):
    """Основные настройки приложения."""
    
    # Telegram
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    
    # Binance
    binance_base_url: str = os.getenv("BINANCE_BASE_URL", "https://api.binance.com")
    
    # Scanner
    scan_interval_minutes: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "5"))
    auto_scan_enabled: bool = os.getenv("AUTO_SCAN_ENABLED", "true").lower() in (
        "1", "true", "yes", "on"
    )
    auto_notify_min_score: int = int(os.getenv("AUTO_NOTIFY_MIN_SCORE", "75"))
    auto_priority_top_limit: int = int(os.getenv("AUTO_PRIORITY_TOP_LIMIT", "20"))
    cryptopanic_auth_token: str = os.getenv("CRYPTOPANIC_AUTH_TOKEN", "")
    cryptopanic_api_plan: str = os.getenv("CRYPTOPANIC_API_PLAN", "developer")
    news_cache_minutes: int = int(os.getenv("NEWS_CACHE_MINUTES", "5"))
    min_signal_score: int = int(os.getenv("MIN_SIGNAL_SCORE", "75"))
    signal_cooldown_minutes: int = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "45"))
    signal_score_change_threshold: int = int(os.getenv("SIGNAL_SCORE_CHANGE_THRESHOLD", "10"))
    scanner_top_limit: int = int(os.getenv("SCANNER_TOP_LIMIT", "50"))
    min_quote_volume_usdt: float = float(os.getenv("MIN_QUOTE_VOLUME_USDT", "5000000"))
    min_drawdown_percent: float = float(os.getenv("MIN_DRAWDOWN_PERCENT", "3"))
    max_drawdown_percent: float = float(os.getenv("MAX_DRAWDOWN_PERCENT", "45"))
    scanner_concurrency: int = int(os.getenv("SCANNER_CONCURRENCY", "8"))
    min_listing_days: int = int(os.getenv("MIN_LISTING_DAYS", "60"))
    signal_validity_minutes: int = int(os.getenv("SIGNAL_VALIDITY_MINUTES", "30"))
    excluded_symbols_csv: str = os.getenv("EXCLUDED_SYMBOLS", "TLMUSDT")

    @property
    def excluded_symbols(self) -> set[str]:
        return {
            item.strip().upper().replace("/", "")
            for item in self.excluded_symbols_csv.split(",")
            if item.strip()
        }
    
    # Database
    database_url: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/bot.db")
    
    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    
    class Config:
        case_sensitive = False


settings = Settings()
