"""Configuration and settings management."""

from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    """Application settings."""

    # Telegram
    telegram_bot_token: str
    
    # Binance
    binance_base_url: str = "https://api.binance.com"
    
    # Scanning
    scan_interval_minutes: int = 5
    
    # Signals
    min_signal_score: int = 75
    signal_cooldown_minutes: int = 45
    signal_score_change_threshold: int = 10
    
    # Database
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"
    
    # Logging
    log_level: str = "INFO"
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


def get_settings() -> Settings:
    """Get application settings."""
    return Settings()
