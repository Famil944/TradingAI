"""Technical indicators calculation."""

import numpy as np
from typing import List
from app.models import CandleData, Indicators
import logging

logger = logging.getLogger(__name__)


class IndicatorCalculator:
    """Calculate technical indicators."""
    
    @staticmethod
    def rsi(prices: List[float], period: int = 14) -> float:
        """Calculate RSI (Relative Strength Index)."""
        if len(prices) < period + 1:
            return 50.0
        
        deltas = np.diff(prices[-period-1:])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return float(rsi)
    
    @staticmethod
    def ema(prices: List[float], period: int) -> float:
        """Calculate EMA (Exponential Moving Average)."""
        if len(prices) < period:
            return float(np.mean(prices))
        
        prices_arr = np.array(prices[-period*2:])
        ema = prices_arr[0]
        multiplier = 2 / (period + 1)
        
        for price in prices_arr[1:]:
            ema = price * multiplier + ema * (1 - multiplier)
        
        return float(ema)
    
    @staticmethod
    def macd(prices: List[float]) -> tuple[float, float, float]:
        """Calculate MACD."""
        if len(prices) < 26:
            return 0.0, 0.0, 0.0
        
        ema12 = IndicatorCalculator.ema(prices, 12)
        ema26 = IndicatorCalculator.ema(prices, 26)
        macd_line = ema12 - ema26
        
        macd_arr = []
        for i in range(max(len(prices) - 26, 1)):
            ema12_i = IndicatorCalculator.ema(prices[:-len(prices)+i+26], 12)
            ema26_i = IndicatorCalculator.ema(prices[:-len(prices)+i+26], 26)
            macd_arr.append(ema12_i - ema26_i)
        
        signal = IndicatorCalculator.ema(macd_arr, 9) if macd_arr else macd_line
        histogram = macd_line - signal
        
        return float(macd_line), float(signal), float(histogram)
    
    @staticmethod
    def bollinger_bands(prices: List[float], period: int = 20, std_dev: float = 2.0) -> tuple[float, float, float, float]:
        """Calculate Bollinger Bands."""
        if len(prices) < period:
            mid = float(np.mean(prices))
            return mid, mid, mid, 0.0
        
        prices_arr = np.array(prices[-period:])
        middle = float(np.mean(prices_arr))
        std = float(np.std(prices_arr))
        upper = middle + (std * std_dev)
        lower = middle - (std * std_dev)
        width = upper - lower
        
        return upper, middle, lower, width
    
    @staticmethod
    def atr(candles: List[CandleData], period: int = 14) -> float:
        """Calculate ATR (Average True Range)."""
        if len(candles) < period:
            return 0.0
        
        true_ranges = []
        for i in range(1, len(candles)):
            high = candles[i].high_price
            low = candles[i].low_price
            prev_close = candles[i-1].close_price
            
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            true_ranges.append(tr)
        
        atr = np.mean(true_ranges[-period:])
        return float(atr)
    
    @staticmethod
    def calculate_all(candles: List[CandleData]) -> Indicators:
        """Calculate all indicators."""
        if not candles:
            raise ValueError("No candles data provided")
        
        closes = [c.close_price for c in candles]
        volumes = [c.volume for c in candles]
        
        # RSI
        rsi = IndicatorCalculator.rsi(closes)
        rsi_30_candles_ago = IndicatorCalculator.rsi(closes[:-30]) if len(closes) > 30 else rsi
        
        # EMA
        ema_20 = IndicatorCalculator.ema(closes, 20)
        ema_50 = IndicatorCalculator.ema(closes, 50)
        ema_200 = IndicatorCalculator.ema(closes, 200)
        
        # MACD
        macd_line, macd_signal, macd_histogram = IndicatorCalculator.macd(closes)
        
        # Bollinger Bands
        bb_upper, bb_middle, bb_lower, bb_width = IndicatorCalculator.bollinger_bands(closes)
        
        # ATR
        atr = IndicatorCalculator.atr(candles)
        atr_percent = (atr / closes[-1]) * 100 if closes[-1] > 0 else 0.0
        
        # Volume
        volume = volumes[-1] if volumes else 0.0
        volume_sma = float(np.mean(volumes[-20:]))
        volume_change = ((volume - volume_sma) / volume_sma * 100) if volume_sma > 0 else 0.0
        
        # Price changes
        current_price = closes[-1]
        price_change_15m = ((closes[-1] - closes[-1]) / closes[-2] * 100) if len(closes) > 1 else 0.0
        price_change_1h = ((closes[-1] - closes[max(-60, -len(closes))]) / closes[max(-60, -len(closes))] * 100) if len(closes) > 60 else 0.0
        price_change_4h = ((closes[-1] - closes[max(-240, -len(closes))]) / closes[max(-240, -len(closes))] * 100) if len(closes) > 240 else 0.0
        price_change_24h = ((closes[-1] - closes[0]) / closes[0] * 100) if closes[0] > 0 else 0.0
        
        return Indicators(
            rsi=rsi,
            rsi_30=rsi_30_candles_ago,
            rsi_1h=rsi,  # Will be recalculated from 1h candles
            rsi_4h=rsi,  # Will be recalculated from 4h candles
            ema_20=ema_20,
            ema_50=ema_50,
            ema_200=ema_200,
            macd_line=macd_line,
            macd_signal=macd_signal,
            macd_histogram=macd_histogram,
            bb_upper=bb_upper,
            bb_middle=bb_middle,
            bb_lower=bb_lower,
            bb_width=bb_width,
            atr=atr,
            atr_percent=atr_percent,
            volume=volume,
            volume_sma=volume_sma,
            volume_change_percent=volume_change,
            price_change_15m=price_change_15m,
            price_change_1h=price_change_1h,
            price_change_4h=price_change_4h,
            price_change_24h=price_change_24h
        )
