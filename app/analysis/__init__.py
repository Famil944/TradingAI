"""Support and resistance level calculation."""

import numpy as np
from typing import List
from app.models import CandleData, SupportResistance
import logging

logger = logging.getLogger(__name__)


class SupportResistanceCalculator:
    """Calculate support and resistance levels."""
    
    @staticmethod
    def find_local_extremes(
        prices: List[float],
        window: int = 5,
        min_distance: float = 0.005
    ) -> tuple[List[float], List[float]]:
        """Find local minima and maxima."""
        if len(prices) < window * 2:
            return [], []
        
        minima = []
        maxima = []
        
        prices_arr = np.array(prices)
        
        for i in range(window, len(prices) - window):
            # Local minimum
            if prices_arr[i] == np.min(prices_arr[i-window:i+window+1]):
                if not minima or abs(prices_arr[i] - minima[-1]) / minima[-1] > min_distance:
                    minima.append(prices_arr[i])
            
            # Local maximum
            if prices_arr[i] == np.max(prices_arr[i-window:i+window+1]):
                if not maxima or abs(prices_arr[i] - maxima[-1]) / maxima[-1] > min_distance:
                    maxima.append(prices_arr[i])
        
        return minima, maxima
    
    @staticmethod
    def cluster_levels(
        levels: List[float],
        threshold_percent: float = 0.5
    ) -> List[float]:
        """Cluster levels that are close together."""
        if not levels:
            return []
        
        sorted_levels = sorted(levels)
        clustered = []
        current_cluster = [sorted_levels[0]]
        
        for level in sorted_levels[1:]:
            distance_percent = abs(level - current_cluster[-1]) / current_cluster[-1] * 100
            if distance_percent < threshold_percent:
                current_cluster.append(level)
            else:
                # Average the cluster
                clustered.append(np.mean(current_cluster))
                current_cluster = [level]
        
        if current_cluster:
            clustered.append(np.mean(current_cluster))
        
        return clustered
    
    @staticmethod
    def calculate(candles: List[CandleData]) -> SupportResistance:
        """Calculate support and resistance levels."""
        if not candles:
            raise ValueError("No candles data provided")
        
        closes = [c.close_price for c in candles]
        current_price = closes[-1]
        
        # Find local extremes
        minima, maxima = SupportResistanceCalculator.find_local_extremes(
            closes,
            window=5,
            min_distance=0.005
        )
        
        # Cluster and get strongest levels
        support_levels = SupportResistanceCalculator.cluster_levels(minima, threshold_percent=0.5)
        resistance_levels = SupportResistanceCalculator.cluster_levels(maxima, threshold_percent=0.5)
        
        # Find nearest support below current price
        support_1 = None
        support_2 = None
        for level in sorted(support_levels, reverse=True):
            if level < current_price:
                if support_1 is None:
                    support_1 = level
                elif support_2 is None:
                    support_2 = level
                    break
        
        if support_1 is None:
            support_1 = current_price * 0.98
        
        # Find nearest resistance above current price
        resistance_1 = None
        resistance_2 = None
        for level in sorted(resistance_levels):
            if level > current_price:
                if resistance_1 is None:
                    resistance_1 = level
                elif resistance_2 is None:
                    resistance_2 = level
                    break
        
        if resistance_1 is None:
            resistance_1 = current_price * 1.02
        
        # Calculate distances
        distance_to_support = ((current_price - support_1) / support_1) * 100
        distance_to_resistance = ((resistance_1 - current_price) / current_price) * 100
        
        return SupportResistance(
            support_1=support_1,
            support_2=support_2,
            resistance_1=resistance_1,
            resistance_2=resistance_2,
            distance_to_support=max(0.0, distance_to_support),
            distance_to_resistance=max(0.0, distance_to_resistance)
        )
