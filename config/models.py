from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List


class CandleData(BaseModel):
    """Данные одной свечи."""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: Optional[int] = None
    taker_buy_quote_volume: Optional[float] = None


class IndicatorValues(BaseModel):
    """Значения индикаторов для одного таймфрейма."""
    rsi: Optional[float] = None
    ema20: Optional[float] = None
    ema50: Optional[float] = None
    ema200: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    atr: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None
    volume_sma: Optional[float] = None


class SupportResistanceLevel(BaseModel):
    """Уровень поддержки/сопротивления."""
    level: float
    type: str  # "support" или "resistance"
    strength: float  # 0-1, мощность уровня
    touches: int  # количество касаний


class MarketData(BaseModel):
    """Полные данные рынка для пары."""
    symbol: str
    current_price: float
    price_change_24h: float
    price_change_percent_24h: float
    
    # Поддержка/сопротивление
    support: Optional[float] = None
    resistance: Optional[float] = None
    support_strength: Optional[float] = None
    resistance_strength: Optional[float] = None
    
    # Индикаторы по таймфреймам
    indicators_5m: Optional[IndicatorValues] = None
    indicators_15m: Optional[IndicatorValues] = None
    indicators_1h: Optional[IndicatorValues] = None
    indicators_4h: Optional[IndicatorValues] = None


class SignalTargets(BaseModel):
    """Уровни ТП для сигнала."""
    tp1: float  # единственная активная цель +3%
    tp2: float  # устаревшее поле совместимости БД
    tp3: float  # устаревшее поле совместимости БД
    tp4: float  # устаревшее поле совместимости БД


class TradeSignal(BaseModel):
    """Торговый сигнал."""
    symbol: str
    current_price: float
    
    # Зона входа
    entry_zone_min: float
    entry_zone_max: float
    
    # Цели
    targets: SignalTargets
    
    # Stop-loss
    stop_loss: float
    stop_loss_percent: float
    
    # Уровни
    support: float
    resistance: float
    
    # Индикаторы
    rsi_5m: Optional[float] = None
    rsi_15m: Optional[float] = None
    rsi_1h: Optional[float] = None
    volume_change_percent: Optional[float] = None
    
    # Score
    score: int
    
    # Причины
    reasons: List[str]
    warnings: List[str]
    
    # Risk/Reward
    risk_reward: float
    tick_size: Optional[float] = None
    
    # Время создания
    created_at: datetime = None
    
    def __init__(self, **data):
        if data.get('created_at') is None:
            data['created_at'] = datetime.utcnow()
        super().__init__(**data)


class SignalResult(BaseModel):
    """Результат отслеживания сигнала."""
    signal_id: int
    symbol: str
    entry_price: float
    max_price: float
    max_favorable_excursion: float
    
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    tp4_hit: bool = False
    stop_hit: bool = False
    
    final_result: Optional[str] = None  # "TP1", "STOP", None


class UserSettings(BaseModel):
    """Настройки пользователя."""
    user_id: int
    min_score: int = 75
    min_target_percent: int = 1  # 1, 2, 3 или 5
    timeframes: List[str] = ["5m", "15m", "1h", "4h"]
    signal_count: int = 10
    auto_notifications: bool = True
