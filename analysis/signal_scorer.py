from typing import Tuple, List, Optional
from config.models import (
    MarketData, TradeSignal, SignalTargets, IndicatorValues, 
    SupportResistanceLevel, CandleData
)
from analysis.indicators import IndicatorCalculator
from analysis.support_resistance import SupportResistanceCalculator
import logging

logger = logging.getLogger(__name__)


class SignalScorer:
    """Расчёт Score сигналов и генерация торговых сигналов."""
    
    # Пороги для различных факторов
    RSI_OVERSOLD = 30
    RSI_WEAK_OVERSOLD = 40
    PRICE_SUPPORT_THRESHOLD_PERCENT = 1.0  # На сколько % от цены считается "рядом с поддержкой"
    BB_RETURN_THRESHOLD = 0.05  # На сколько % цена должна вернуться в Bollinger Bands
    VOLUME_THRESHOLD_INCREASE = 30  # На сколько % должен вырасти объём
    
    @staticmethod
    def calculate_score(
        market_data: MarketData,
        indicators_5m: IndicatorValues,
        indicators_15m: IndicatorValues,
        indicators_1h: IndicatorValues,
        indicators_4h: IndicatorValues,
        support: Optional[float] = None,
        resistance: Optional[float] = None,
        volume_sma: Optional[float] = None,
        current_volume: Optional[float] = None
    ) -> Tuple[int, List[str], List[str]]:
        """
        Расчёт Score сигнала (0-100).
        
        Returns:
            (score, reasons_list, warnings_list)
        """
        score = 20
        reasons = []
        warnings = []
        
        # 1. RSI фактор (0-20 пунктов)
        rsi_5m = indicators_5m.rsi if indicators_5m else None
        rsi_15m = indicators_15m.rsi if indicators_15m else None
        
        if rsi_5m is not None:
            if rsi_5m < SignalScorer.RSI_OVERSOLD:
                score += 20
                reasons.append(f"✅ RSI 5m в перепроданности ({rsi_5m:.1f})")
            elif rsi_5m < SignalScorer.RSI_WEAK_OVERSOLD:
                score += 10
                reasons.append(f"✅ RSI 5m слегка пересолден ({rsi_5m:.1f})")
        
        if rsi_15m is not None:
            if rsi_15m < SignalScorer.RSI_OVERSOLD:
                score += 5
                reasons.append(f"✅ RSI 15m в перепроданности ({rsi_15m:.1f})")
        
        # 2. Поддержка фактор (0-15 пунктов)
        if support and market_data.current_price:
            percent_to_support = ((market_data.current_price - support) / support) * 100
            if percent_to_support <= SignalScorer.PRICE_SUPPORT_THRESHOLD_PERCENT:
                score += 15
                reasons.append(f"✅ Цена рядом с поддержкой ({percent_to_support:.2f}%)")
            elif percent_to_support <= SignalScorer.PRICE_SUPPORT_THRESHOLD_PERCENT * 2:
                score += 8
                warnings.append(f"⚠️ Поддержка на расстоянии {percent_to_support:.2f}%")
        
        # 3. MACD фактор (0-15 пунктов)
        macd_5m = indicators_5m.macd_hist if indicators_5m else None
        macd_15m = indicators_15m.macd_hist if indicators_15m else None
        
        if macd_5m is not None:
            if macd_5m > 0 and macd_5m < 0.001:  # Ослабление падения или bullish cross
                score += 10
                reasons.append(f"✅ MACD 5m показывает ослабление падения")
            elif macd_5m > 0:
                score += 5
                reasons.append(f"✅ MACD 5m выше нулевой линии")
        
        if macd_15m is not None and macd_15m > 0:
            score += 5
            reasons.append(f"✅ MACD 15m позитивен")
        
        # 4. Объём фактор (0-10 пунктов)
        if volume_sma and current_volume:
            volume_increase = ((current_volume - volume_sma) / volume_sma) * 100
            if volume_increase >= SignalScorer.VOLUME_THRESHOLD_INCREASE:
                score += 10
                reasons.append(f"✅ Объём выше среднего на {volume_increase:.1f}%")
            elif volume_increase >= SignalScorer.VOLUME_THRESHOLD_INCREASE / 2:
                score += 5
        
        # 5. Bollinger Bands фактор (0-10 пунктов)
        if indicators_5m and indicators_5m.bb_lower and indicators_5m.bb_upper:
            current_price = market_data.current_price
            bb_lower = indicators_5m.bb_lower
            bb_upper = indicators_5m.bb_upper
            
            # Если цена ниже нижней линии и вернулась в диапазон
            if current_price > bb_lower and current_price < bb_upper:
                bb_position = (current_price - bb_lower) / (bb_upper - bb_lower)
                if bb_position < 0.3:  # Цена в нижней части диапазона
                    score += 10
                    reasons.append(f"✅ Цена в нижней части Bollinger Bands")
        
        # 6. EMA фактор (0-10 пунктов)
        if indicators_5m and indicators_5m.ema20:
            current_price = market_data.current_price
            ema20 = indicators_5m.ema20
            
            if current_price > ema20:
                score += 5
                reasons.append(f"✅ Цена выше EMA20")
            
            if indicators_15m and indicators_15m.ema20:
                # Если EMA20 разворачивается вверх (следующее значение > предыдущее)
                # Здесь мы не можем это проверить без истории, поэтому пропускаем
                pass
        
        # Старший таймфрейм не должен подтверждать ускоряющееся падение.
        higher_rsi = [
            item.rsi for item in (indicators_1h, indicators_4h)
            if item and item.rsi is not None
        ]
        if higher_rsi and min(higher_rsi) < 22:
            score -= 10
            warnings.append("⚠️ Сильное падение на старшем таймфрейме")
        elif higher_rsi and all(value < 48 for value in higher_rsi):
            score += 5
            reasons.append("✅ Старшие таймфреймы ещё в зоне восстановления")

        # Ограничиваем score диапазоном 0-100
        score = max(0, min(score, 100))
        score = min(score, 100)
        
        return score, reasons, warnings
    
    @staticmethod
    def calculate_tp_levels(
        entry_price: float,
        atr: Optional[float] = None
    ) -> SignalTargets:
        """
        Единственная цель спотовой стратегии: +3%.
        """
        return SignalTargets(
            tp1=entry_price * 1.03,
            # Поля сохранены для совместимости со старой БД, но дополнительных
            # целей в стратегии больше нет.
            tp2=entry_price * 1.03,
            tp3=entry_price * 1.03,
            tp4=entry_price * 1.03,
        )
    
    @staticmethod
    def calculate_entry_zone(
        support: float,
        current_price: float
    ) -> Tuple[float, float]:
        """
        Расчёт зоны входа.
        Зона = от поддержки до текущей цены (±1-2%).
        """
        # Не предлагаем ловить заявку далеко ниже рынка: вход только после
        # подтверждения около текущей цены.
        zone_min = max(support, current_price * 0.995)
        zone_max = current_price * 1.003
        
        return zone_min, zone_max
    
    @staticmethod
    def calculate_stop_loss(
        entry_price: float,
        support: float
    ) -> Tuple[float, float]:
        """
        Расчёт Stop-Loss.
        Stop-loss = поддержка - 2% от цены входа.
        
        Returns:
            (stop_price, stop_percent)
        """
        stop_loss = support * 0.985
        percent_diff = ((entry_price - stop_loss) / entry_price) * 100
        
        return stop_loss, percent_diff
    
    @staticmethod
    def calculate_risk_reward(
        entry_price: float,
        stop_loss: float,
        tp_price: float
    ) -> float:
        """Расчёт Risk/Reward."""
        risk = entry_price - stop_loss
        reward = tp_price - entry_price
        
        if risk <= 0:
            return 0
        
        return reward / risk
    
    @staticmethod
    def generate_signal(
        symbol: str,
        market_data: MarketData,
        candles: List[CandleData],
        min_score: int = 75,
        candles_15m: Optional[List[CandleData]] = None,
        candles_1h: Optional[List[CandleData]] = None,
        candles_4h: Optional[List[CandleData]] = None,
        min_drawdown_percent: float = 3.0,
        max_drawdown_percent: float = 45.0,
    ) -> Optional[TradeSignal]:
        """
        Генерация торгового сигнала на основе анализа рынка.
        
        Returns:
            TradeSignal или None если score < min_score
        """
        if not market_data.current_price:
            return None
        
        candles_15m = candles_15m or candles
        candles_1h = candles_1h or candles
        candles_4h = candles_4h or candles
        indicators = IndicatorCalculator.get_all_indicators(candles)
        indicators_15m = IndicatorCalculator.get_all_indicators(candles_15m)
        indicators_1h = IndicatorCalculator.get_all_indicators(candles_1h)
        indicators_4h = IndicatorCalculator.get_all_indicators(candles_4h)

        period_high = max((item.high for item in candles_4h), default=0)
        if period_high <= 0:
            return None
        drawdown = (period_high - market_data.current_price) / period_high * 100
        if drawdown < min_drawdown_percent or drawdown > max_drawdown_percent:
            return None

        # Не ловим свободное падение: последняя закрытая свеча должна показать
        # откуп либо цена должна уже вернуть EMA20 на 5m.
        last = candles[-1]
        bullish_rejection = last.close > last.open and last.close > (last.low + (last.high - last.low) * 0.55)
        ema_reclaim = indicators.ema20 is not None and last.close >= indicators.ema20
        if not (bullish_rejection or ema_reclaim):
            return None
        
        # Поддержка/сопротивление
        support_level, resistance_level = SupportResistanceCalculator.get_nearest_support_resistance(
            market_data.current_price,
            candles
        )
        
        # Используем значения по умолчанию если уровни не найдены
        if not support_level:
            support = market_data.current_price * 0.97  # 3% ниже текущей цены
            logger.debug(f"{symbol}: поддержка не найдена, используем {support:.2f}")
        else:
            support = support_level.level
        
        if not resistance_level:
            resistance = market_data.current_price * 1.05  # 5% выше текущей цены
            logger.debug(f"{symbol}: сопротивление не найдено, используем {resistance:.2f}")
        else:
            resistance = resistance_level.level
        
        # Расчёт Score
        score, reasons, warnings = SignalScorer.calculate_score(
            market_data,
            indicators,
            indicators_15m,
            indicators_1h,
            indicators_4h,
            support=support,
            resistance=resistance,
            volume_sma=indicators.volume_sma,
            current_volume=candles[-1].volume if candles else None
        )
        if drawdown >= 8:
            score += min(15, int(drawdown / 2))
            reasons.append(f"✅ Просадка от локального максимума: {drawdown:.1f}%")
        else:
            score += 4
            reasons.append(f"✅ Умеренная просадка: {drawdown:.1f}%")
        if bullish_rejection:
            score += 8
            reasons.append("✅ Последняя свеча показывает откуп")
        score = min(score, 100)
        
        if score < min_score:
            logger.debug(f"{symbol}: Score {score} < {min_score}, сигнал не создан")
            return None
        
        # Расчёт зоны входа
        entry_min, entry_max = SignalScorer.calculate_entry_zone(support, market_data.current_price)
        
        # Расчёт TP-уровней
        targets = SignalScorer.calculate_tp_levels(market_data.current_price, indicators.atr)
        
        # Расчёт Stop-Loss
        stop_loss, stop_percent = SignalScorer.calculate_stop_loss(market_data.current_price, support)
        
        risk_reward = SignalScorer.calculate_risk_reward(
            market_data.current_price, stop_loss, targets.tp1
        )
        if risk_reward < 1.2:
            return None
        
        # Создаём сигнал
        signal = TradeSignal(
            symbol=symbol,
            current_price=market_data.current_price,
            entry_zone_min=entry_min,
            entry_zone_max=entry_max,
            targets=targets,
            stop_loss=stop_loss,
            stop_loss_percent=stop_percent,
            support=support,
            resistance=resistance,
            rsi_5m=indicators.rsi,
            rsi_15m=indicators_15m.rsi,
            rsi_1h=indicators_1h.rsi,
            volume_change_percent=(
                ((candles[-1].volume - indicators.volume_sma) / indicators.volume_sma * 100)
                if indicators.volume_sma and candles else None
            ),
            score=score,
            reasons=reasons,
            warnings=warnings,
            risk_reward=risk_reward
        )
        
        return signal
