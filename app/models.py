"""Data models for signals, trades, and market data."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class CandleData(BaseModel):
    """OHLCV candle data."""
    
    open_time: int
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float
    close_time: int
    quote_asset_volume: float
    number_of_trades: int
    taker_buy_base_volume: float
    taker_buy_quote_volume: float


class Indicators(BaseModel):
    """Technical indicators values."""
    
    rsi: float
    rsi_30: float = Field(description="RSI value 30 minutes ago")
    rsi_1h: float = Field(description="RSI on 1h timeframe")
    rsi_4h: float = Field(description="RSI on 4h timeframe")
    
    ema_20: float
    ema_50: float
    ema_200: float
    
    macd_line: float
    macd_signal: float
    macd_histogram: float
    
    bb_upper: float
    bb_middle: float
    bb_lower: float
    bb_width: float
    
    atr: float
    atr_percent: float
    
    volume: float
    volume_sma: float
    volume_change_percent: float
    
    price_change_15m: float = Field(description="% change in 15 minutes")
    price_change_1h: float = Field(description="% change in 1 hour")
    price_change_4h: float = Field(description="% change in 4 hours")
    price_change_24h: float = Field(description="% change in 24 hours")


class SupportResistance(BaseModel):
    """Support and resistance levels."""
    
    support_1: float
    support_2: Optional[float] = None
    resistance_1: float
    resistance_2: Optional[float] = None
    distance_to_support: float = Field(description="% distance to nearest support")
    distance_to_resistance: float = Field(description="% distance to nearest resistance")


class SignalReason(BaseModel):
    """Reason for signal with checkmark."""
    
    reason: str
    is_positive: bool = True  # ✅ if True, ⚠️ if False


class TakeProfitLevel(BaseModel):
    """Take profit level."""
    
    level: float
    percent: float
    price: float


class Signal(BaseModel):
    """Trading signal."""
    
    id: Optional[int] = None
    symbol: str
    current_price: float
    
    entry_zone_low: float
    entry_zone_high: float
    
    tp1: TakeProfitLevel = Field(description="TP1 ~+1%")
    tp2: TakeProfitLevel = Field(description="TP2 ~+2%")
    tp3: TakeProfitLevel = Field(description="TP3 ~+3%")
    tp4: TakeProfitLevel = Field(description="TP4 ~+5%")
    
    stop_loss_price: float
    stop_loss_percent: float
    
    risk_reward: float
    score: int = Field(ge=0, le=100)
    
    indicators: Indicators
    support_resistance: SupportResistance
    reasons: list[SignalReason]
    
    timeframe: str = Field(default="5m", description="Primary timeframe")
    created_at: datetime
    
    disclaimer: str = Field(
        default="Signals are based on technical analysis and are NOT a guarantee of price movement or financial advice."
    )


class SignalResult(BaseModel):
    """Signal result after tracking."""
    
    signal_id: int
    symbol: str
    entry_price: float
    
    max_price: float
    max_price_percent: float
    
    tp1_reached: bool = False
    tp2_reached: bool = False
    tp3_reached: bool = False
    tp4_reached: bool = False
    stop_hit: bool = False
    
    tracked_at: datetime
    closed_at: Optional[datetime] = None


class UserSettings(BaseModel):
    """User settings."""
    
    user_id: int
    min_score: int = 75
    min_tp_percent: int = 1  # Minimum TP (1%, 2%, 3%, or 5%)
    timeframes: list[str] = ["5m", "15m", "1h", "4h"]
    max_signals_displayed: int = 10
    auto_notifications_enabled: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
