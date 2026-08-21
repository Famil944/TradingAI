from typing import List, Optional, Tuple
from config.models import CandleData, SupportResistanceLevel
import numpy as np
import logging

logger = logging.getLogger(__name__)


class SupportResistanceCalculator:
    """Расчёт уровней поддержки и сопротивления методом локальных экстремумов."""
    
    @staticmethod
    def find_local_extrema(candles: List[CandleData], window: int = 5) -> Tuple[List[float], List[float]]:
        """
        Найти локальные минимумы и максимумы.
        
        Args:
            candles: список свечей
            window: окно для поиска экстремума (по умолчанию 5 свечей)
        
        Returns:
            (список низов, список вершин)
        """
        if len(candles) < window:
            return [], []
        
        lows = [c.low for c in candles]
        highs = [c.high for c in candles]
        
        local_lows = []
        local_highs = []
        
        for i in range(window, len(candles) - window):
            # Проверяем локальный минимум
            is_local_low = lows[i] == min(lows[i - window:i + window + 1])
            if is_local_low:
                local_lows.append(lows[i])
            
            # Проверяем локальный максимум
            is_local_high = highs[i] == max(highs[i - window:i + window + 1])
            if is_local_high:
                local_highs.append(highs[i])
        
        return local_lows, local_highs
    
    @staticmethod
    def cluster_levels(levels: List[float], threshold_percent: float = 0.5) -> List[float]:
        """
        Кластеризация близких уровней.
        Объединяет уровни, которые находятся в пределах threshold_percent друг от друга.
        
        Args:
            levels: список ценовых уровней
            threshold_percent: порог близости в процентах
        
        Returns:
            список кластеризованных уровней
        """
        if not levels:
            return []
        
        levels = sorted(levels)
        clusters = []
        current_cluster = [levels[0]]
        
        for level in levels[1:]:
            # Проверяем расстояние от первого уровня в кластере
            first_level = current_cluster[0]
            percent_diff = abs(level - first_level) / first_level * 100
            
            if percent_diff <= threshold_percent:
                current_cluster.append(level)
            else:
                # Завершаем текущий кластер и начинаем новый
                clusters.append(np.mean(current_cluster))
                current_cluster = [level]
        
        # Добавляем последний кластер
        if current_cluster:
            clusters.append(np.mean(current_cluster))
        
        return clusters
    
    @staticmethod
    def calculate_level_strength(levels: List[float], threshold_percent: float = 0.5) -> dict:
        """
        Рассчитать мощность уровня на основе количества касаний.
        
        Args:
            levels: список ценовых уровней
            threshold_percent: порог близости для засчитывания касания
        
        Returns:
            словарь {уровень: (мощность 0-1, количество касаний)}
        """
        level_touches = {}
        
        for level in levels:
            touches = 0
            for other in levels:
                percent_diff = abs(level - other) / level * 100
                if percent_diff <= threshold_percent:
                    touches += 1
            
            # Нормализуем мощность от 0 до 1
            # Максимум касаний = количество уровней
            strength = min(touches / max(len(levels) / 3, 1), 1.0)
            level_touches[level] = (strength, touches)
        
        return level_touches
    
    @staticmethod
    def get_nearest_support_resistance(
        current_price: float,
        candles: List[CandleData],
        lookback: int = 50
    ) -> Tuple[Optional[SupportResistanceLevel], Optional[SupportResistanceLevel]]:
        """
        Найти ближайшие уровни поддержки и сопротивления.
        
        Args:
            current_price: текущая цена
            candles: список свечей для анализа
            lookback: количество свечей для анализа
        
        Returns:
            (ближайший уровень поддержки, ближайший уровень сопротивления)
        """
        if len(candles) < lookback:
            candles_for_analysis = candles
        else:
            candles_for_analysis = candles[-lookback:]
        
        local_lows, local_highs = SupportResistanceCalculator.find_local_extrema(candles_for_analysis)
        
        # Кластеризуем уровни
        support_levels = SupportResistanceCalculator.cluster_levels(local_lows)
        resistance_levels = SupportResistanceCalculator.cluster_levels(local_highs)
        
        # Рассчитываем мощность уровней
        support_strength = SupportResistanceCalculator.calculate_level_strength(support_levels)
        resistance_strength = SupportResistanceCalculator.calculate_level_strength(resistance_levels)
        
        # Ищем ближайшие уровни
        nearest_support = None
        nearest_resistance = None
        
        # Поддержка - максимальный уровень ниже текущей цены
        supports_below = [lvl for lvl in support_levels if lvl < current_price]
        if supports_below:
            best_support = max(supports_below)  # Максимум из уровней ниже
            strength, touches = support_strength.get(best_support, (0.0, 0))
            nearest_support = SupportResistanceLevel(
                level=best_support,
                type="support",
                strength=strength,
                touches=touches
            )
        
        # Сопротивление - минимальный уровень выше текущей цены
        resistances_above = [lvl for lvl in resistance_levels if lvl > current_price]
        if resistances_above:
            best_resistance = min(resistances_above)  # Минимум из уровней выше
            strength, touches = resistance_strength.get(best_resistance, (0.0, 0))
            nearest_resistance = SupportResistanceLevel(
                level=best_resistance,
                type="resistance",
                strength=strength,
                touches=touches
            )
        
        return nearest_support, nearest_resistance
