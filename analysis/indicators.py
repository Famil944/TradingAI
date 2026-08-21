import numpy as np
from typing import List, Optional, Tuple
from config.models import CandleData, IndicatorValues
import logging

logger = logging.getLogger(__name__)


class IndicatorCalculator:
    """Расчёт технических индикаторов."""
    
    @staticmethod
    def calculate_rsi(closes: List[float], period: int = 14) -> Optional[float]:
        """Расчёт RSI (14)."""
        if len(closes) < period + 1:
            return None
        
        closes = np.array(closes, dtype=np.float64)
        deltas = np.diff(closes)
        seed = deltas[:period + 1]
        
        up = seed[seed >= 0].sum() / period
        down = -seed[seed < 0].sum() / period
        if down == 0:
            rsi = 100.0 if up > 0 else 50.0
        else:
            rs = up / down
            rsi = 100.0 - 100.0 / (1.0 + rs)
        
        for delta in deltas[period + 1:]:
            up = (up * (period - 1) + (delta if delta > 0 else 0)) / period
            down = (down * (period - 1) + (-delta if delta < 0 else 0)) / period
            if down == 0:
                rsi = 100.0 if up > 0 else 50.0
            else:
                rs = up / down
                rsi = 100.0 - 100.0 / (1.0 + rs)
        
        return rsi
    
    @staticmethod
    def calculate_ema(closes: List[float], period: int) -> Optional[float]:
        """Расчёт EMA."""
        if len(closes) < period:
            return None
        
        closes = np.array(closes, dtype=np.float64)
        ema = np.mean(closes[:period])
        multiplier = 2.0 / (period + 1)
        
        for price in closes[period:]:
            ema = (price - ema) * multiplier + ema
        
        return ema
    
    @staticmethod
    def calculate_macd(closes: List[float]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """Расчёт MACD."""
        if len(closes) < 26:
            return None, None, None
        
        closes = np.array(closes, dtype=np.float64)
        
        ema12 = IndicatorCalculator.calculate_ema(closes.tolist(), 12)
        ema26 = IndicatorCalculator.calculate_ema(closes.tolist(), 26)
        
        if ema12 is None or ema26 is None:
            return None, None, None
        
        macd = ema12 - ema26
        
        # Signal line (EMA9 of MACD)
        macd_line = []
        for i in range(len(closes) - 25):
            ema12_i = IndicatorCalculator.calculate_ema(closes[:i + 26].tolist(), 12)
            ema26_i = IndicatorCalculator.calculate_ema(closes[:i + 26].tolist(), 26)
            if ema12_i and ema26_i:
                macd_line.append(ema12_i - ema26_i)
        
        signal = IndicatorCalculator.calculate_ema(macd_line, 9) if len(macd_line) >= 9 else None
        hist = (macd - signal) if signal else None
        
        return macd, signal, hist
    
    @staticmethod
    def calculate_atr(candles: List[CandleData], period: int = 14) -> Optional[float]:
        """Расчёт ATR (Average True Range)."""
        if len(candles) < period:
            return None
        
        tr_values = []
        for i in range(1, len(candles)):
            high = candles[i].high
            low = candles[i].low
            prev_close = candles[i - 1].close
            
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            tr_values.append(tr)
        
        if len(tr_values) < period:
            return None
        
        atr = np.mean(tr_values[:period])
        multiplier = 2.0 / (period + 1)
        
        for tr in tr_values[period:]:
            atr = (tr - atr) * multiplier + atr
        
        return atr
    
    @staticmethod
    def calculate_bollinger_bands(closes: List[float], period: int = 20, std_dev: float = 2.0) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """Расчёт Bollinger Bands."""
        if len(closes) < period:
            return None, None, None
        
        closes = np.array(closes, dtype=np.float64)
        middle = np.mean(closes[-period:])
        std = np.std(closes[-period:])
        
        upper = middle + (std * std_dev)
        lower = middle - (std * std_dev)
        
        return upper, middle, lower
    
    @staticmethod
    def calculate_volume_sma(volumes: List[float], period: int = 20) -> Optional[float]:
        """Расчёт SMA объёма."""
        if len(volumes) < period:
            return None
        
        return np.mean(volumes[-period:])
    
    @staticmethod
    def get_all_indicators(candles: List[CandleData]) -> IndicatorValues:
        """Расчёт всех индикаторов для набора свечей."""
        if not candles:
            return IndicatorValues()
        
        closes = [c.close for c in candles]
        volumes = [c.volume for c in candles]
        
        rsi = IndicatorCalculator.calculate_rsi(closes)
        ema20 = IndicatorCalculator.calculate_ema(closes, 20)
        ema50 = IndicatorCalculator.calculate_ema(closes, 50)
        ema200 = IndicatorCalculator.calculate_ema(closes, 200)
        macd, macd_signal, macd_hist = IndicatorCalculator.calculate_macd(closes)
        atr = IndicatorCalculator.calculate_atr(candles)
        bb_upper, bb_middle, bb_lower = IndicatorCalculator.calculate_bollinger_bands(closes)
        volume_sma = IndicatorCalculator.calculate_volume_sma(volumes)
        
        return IndicatorValues(
            rsi=rsi,
            ema20=ema20,
            ema50=ema50,
            ema200=ema200,
            macd=macd,
            macd_signal=macd_signal,
            macd_hist=macd_hist,
            atr=atr,
            bb_upper=bb_upper,
            bb_middle=bb_middle,
            bb_lower=bb_lower,
            volume_sma=volume_sma
        )
