"""Signal scoring system."""

from typing import List, Tuple
from app.models import (
    Indicators, SupportResistance, Signal, SignalReason,
    TakeProfitLevel, CandleData
)
from app.indicators import IndicatorCalculator
from app.analysis import SupportResistanceCalculator
import logging

logger = logging.getLogger(__name__)


class SignalScorer:
    """Score and generate trading signals."""
    
    # Score factor weights
    RSI_WEIGHT = 20
    SUPPORT_WEIGHT = 15
    MACD_WEIGHT = 15
    VOLUME_WEIGHT = 10
    BB_WEIGHT = 10
    EMA_WEIGHT = 10
    BASE_SCORE = 40
    
    @staticmethod
    def calculate_score(
        indicators: Indicators,
        support_resistance: SupportResistance,
        current_price: float
    ) -> Tuple[int, List[SignalReason]]:
        """Calculate signal score based on indicators."""
        score = SignalScorer.BASE_SCORE
        reasons = []
        
        # RSI Factor (0-20)
        if indicators.rsi < 30:
            score += SignalScorer.RSI_WEIGHT
            reasons.append(SignalReason(
                reason="RSI in oversold zone (< 30)",
                is_positive=True
            ))
        elif indicators.rsi < 40:
            score += SignalScorer.RSI_WEIGHT // 2
            reasons.append(SignalReason(
                reason="RSI showing weakness (30-40)",
                is_positive=True
            ))
        elif indicators.rsi < 50:
            reasons.append(SignalReason(
                reason="RSI neutral (40-50)",
                is_positive=True
            ))
        else:
            reasons.append(SignalReason(
                reason="RSI above midpoint",
                is_positive=False
            ))
        
        # Support Factor (0-15)
        if support_resistance.distance_to_support <= 1.0:
            score += SignalScorer.SUPPORT_WEIGHT
            reasons.append(SignalReason(
                reason="Price near strong support",
                is_positive=True
            ))
        elif support_resistance.distance_to_support <= 2.0:
            score += SignalScorer.SUPPORT_WEIGHT // 2
            reasons.append(SignalReason(
                reason="Price near support",
                is_positive=True
            ))
        else:
            reasons.append(SignalReason(
                reason="Far from support",
                is_positive=False
            ))
        
        # MACD Factor (0-15)
        if indicators.macd_histogram > 0 and indicators.macd_line > indicators.macd_signal:
            score += SignalScorer.MACD_WEIGHT
            reasons.append(SignalReason(
                reason="MACD bullish crossover or increasing",
                is_positive=True
            ))
        elif indicators.macd_histogram > -0.0001:
            score += SignalScorer.MACD_WEIGHT // 3
            reasons.append(SignalReason(
                reason="MACD showing weakening downtrend",
                is_positive=True
            ))
        else:
            reasons.append(SignalReason(
                reason="MACD still bearish",
                is_positive=False
            ))
        
        # Volume Factor (0-10)
        if indicators.volume_change_percent >= 30:
            score += SignalScorer.VOLUME_WEIGHT
            reasons.append(SignalReason(
                reason="Volume significantly above average (+30%)",
                is_positive=True
            ))
        elif indicators.volume_change_percent >= 10:
            score += SignalScorer.VOLUME_WEIGHT // 2
            reasons.append(SignalReason(
                reason="Volume above average",
                is_positive=True
            ))
        else:
            reasons.append(SignalReason(
                reason="Volume below average",
                is_positive=False
            ))
        
        # Bollinger Bands Factor (0-10)
        if current_price <= indicators.bb_lower:
            score += SignalScorer.BB_WEIGHT
            reasons.append(SignalReason(
                reason="Price at lower Bollinger Band",
                is_positive=True
            ))
        elif current_price < indicators.bb_middle:
            score += SignalScorer.BB_WEIGHT // 2
            reasons.append(SignalReason(
                reason="Price in lower half of Bollinger Bands",
                is_positive=True
            ))
        else:
            reasons.append(SignalReason(
                reason="Price in upper half of bands",
                is_positive=False
            ))
        
        # EMA Factor (0-10)
        if current_price > indicators.ema_20 and current_price > indicators.ema_50:
            score += SignalScorer.EMA_WEIGHT // 2
            reasons.append(SignalReason(
                reason="Price above EMA20 and EMA50",
                is_positive=True
            ))
        elif current_price > indicators.ema_20:
            score += SignalScorer.EMA_WEIGHT // 3
            reasons.append(SignalReason(
                reason="Price above EMA20",
                is_positive=True
            ))
        else:
            reasons.append(SignalReason(
                reason="Price below key EMAs",
                is_positive=False
            ))
        
        # Ensure score is in valid range
        score = max(0, min(100, score))
        
        return score, reasons
    
    @staticmethod
    def calculate_tp_levels(
        entry_price: float,
        atr: float,
        resistance_1: float,
        current_price: float
    ) -> Tuple[TakeProfitLevel, TakeProfitLevel, TakeProfitLevel, TakeProfitLevel]:
        """Calculate take profit levels based on ATR."""
        # Target percentages
        tp1_percent = 1.0
        tp2_percent = 2.0
        tp3_percent = 3.0
        tp4_percent = 5.0
        
        # ATR adjustment factor (scale TP levels based on volatility)
        atr_factor = 1.0 + (atr / current_price * 0.5)
        
        tp1 = entry_price * (1 + tp1_percent / 100 * atr_factor / 1.0)
        tp2 = entry_price * (1 + tp2_percent / 100 * atr_factor / 1.0)
        tp3 = entry_price * (1 + tp3_percent / 100 * atr_factor / 1.0)
        tp4 = entry_price * (1 + tp4_percent / 100 * atr_factor / 1.0)
        
        return (
            TakeProfitLevel(level=tp1, percent=tp1_percent, price=tp1),
            TakeProfitLevel(level=tp2, percent=tp2_percent, price=tp2),
            TakeProfitLevel(level=tp3, percent=tp3_percent, price=tp3),
            TakeProfitLevel(level=tp4, percent=tp4_percent, price=tp4),
        )
    
    @staticmethod
    def generate_signal(
        symbol: str,
        candles_5m: List[CandleData],
        candles_1h: List[CandleData],
        candles_4h: List[CandleData]
    ) -> Signal | None:
        """Generate signal if conditions are met."""
        if not candles_5m:
            return None
        
        try:
            # Calculate indicators
            indicators_5m = IndicatorCalculator.calculate_all(candles_5m)
            indicators_1h = IndicatorCalculator.calculate_all(candles_1h) if candles_1h else indicators_5m
            indicators_4h = IndicatorCalculator.calculate_all(candles_4h) if candles_4h else indicators_5m
            
            # Update RSI from different timeframes
            indicators_5m.rsi_1h = indicators_1h.rsi
            indicators_5m.rsi_4h = indicators_4h.rsi
            
            # Calculate support/resistance
            sr = SupportResistanceCalculator.calculate(candles_5m)
            
            # Calculate score
            current_price = candles_5m[-1].close_price
            score, reasons = SignalScorer.calculate_score(
                indicators_5m, sr, current_price
            )
            
            # Filter out weak signals
            if score < 60:
                return None
            
            # Entry zone around support
            entry_zone_low = sr.support_1 * 0.99
            entry_zone_high = sr.support_1 * 1.01
            
            # Calculate TP levels
            atr = IndicatorCalculator.atr(candles_5m)
            tp1, tp2, tp3, tp4 = SignalScorer.calculate_tp_levels(
                entry_zone_high, atr, sr.resistance_1, current_price
            )
            
            # Stop loss (2% below support)
            stop_loss_price = sr.support_1 * 0.98
            stop_loss_percent = ((stop_loss_price - entry_zone_high) / entry_zone_high) * 100
            
            # Risk/Reward ratio
            avg_tp = (tp1.price + tp2.price + tp3.price + tp4.price) / 4
            potential_profit = avg_tp - entry_zone_high
            potential_loss = entry_zone_high - stop_loss_price
            risk_reward = potential_profit / potential_loss if potential_loss > 0 else 1.0
            
            from datetime import datetime
            
            return Signal(
                symbol=symbol,
                current_price=current_price,
                entry_zone_low=entry_zone_low,
                entry_zone_high=entry_zone_high,
                tp1=tp1,
                tp2=tp2,
                tp3=tp3,
                tp4=tp4,
                stop_loss_price=stop_loss_price,
                stop_loss_percent=stop_loss_percent,
                risk_reward=risk_reward,
                score=score,
                indicators=indicators_5m,
                support_resistance=sr,
                reasons=reasons,
                timeframe="5m",
                created_at=datetime.utcnow()
            )
        
        except Exception as e:
            logger.error(f"Error generating signal for {symbol}: {e}")
            return None
