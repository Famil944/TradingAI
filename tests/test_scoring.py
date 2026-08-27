import unittest
from analysis.support_resistance import SupportResistanceCalculator
from config.models import CandleData


class TestSupportResistance(unittest.TestCase):
    """Тесты для расчёта поддержки/сопротивления."""
    
    def test_find_local_extrema(self):
        """Тест поиска локальных экстремумов."""
        candles = []
        prices = [100, 102, 104, 103, 105, 107, 106, 108, 110, 109, 111, 112, 111, 110, 109]
        
        for i, price in enumerate(prices):
            candles.append(CandleData(
                timestamp=i,
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                volume=1000
            ))
        
        lows, highs = SupportResistanceCalculator.find_local_extrema(candles, window=2)
        
        self.assertTrue(len(lows) > 0 or len(highs) > 0)
    
    def test_cluster_levels(self):
        """Тест кластеризации уровней."""
        levels = [100, 100.2, 100.3, 110, 110.1, 110.2]
        
        clustered = SupportResistanceCalculator.cluster_levels(levels, threshold_percent=0.5)
        
        # Должны быть созданы 2 кластера около 100 и 110
        self.assertEqual(len(clustered), 2)
    
    def test_level_strength(self):
        """Тест расчёта мощности уровня."""
        levels = [100, 100.2, 100.3, 110, 110.1]
        
        strength_data = SupportResistanceCalculator.calculate_level_strength(levels, threshold_percent=0.5)
        
        # Проверяем структуру результата
        self.assertTrue(len(strength_data) > 0)
        
        for level, (strength, touches) in strength_data.items():
            self.assertGreaterEqual(strength, 0)
            self.assertLessEqual(strength, 1)
            self.assertGreater(touches, 0)


class TestScoringLogic(unittest.TestCase):
    """Тесты для логики скоринга сигналов."""
    
    def test_tp_levels_calculation(self):
        """Тест расчёта TP-уровней."""
        from analysis.signal_scorer import SignalScorer
        
        entry_price = 100
        targets = SignalScorer.calculate_tp_levels(entry_price, atr=0.5)
        
        # Активна только одна цель +3%; остальные поля — совместимость БД.
        self.assertGreater(targets.tp1, entry_price)
        for actual, expected in zip(
            [targets.tp1, targets.tp2, targets.tp3, targets.tp4],
            [103.0, 103.0, 103.0, 103.0],
        ):
            self.assertAlmostEqual(actual, expected)
    
    def test_entry_zone_calculation(self):
        """Тест расчёта зоны входа."""
        from analysis.signal_scorer import SignalScorer
        
        support = 95
        current_price = 100
        
        zone_min, zone_max = SignalScorer.calculate_entry_zone(support, current_price)
        
        # Зона входа должна быть ниже или около поддержки
        self.assertLess(zone_min, current_price)
        self.assertLessEqual(zone_max, current_price * 1.02)
    
    def test_stop_loss_calculation(self):
        """Тест расчёта Stop-Loss."""
        from analysis.signal_scorer import SignalScorer
        
        entry_price = 100
        support = 95
        
        stop_loss, stop_percent = SignalScorer.calculate_stop_loss(entry_price, support)
        
        # Stop должен быть ниже поддержки
        self.assertLess(stop_loss, support)
        self.assertGreater(stop_percent, 0)
    
    def test_risk_reward_calculation(self):
        """Тест расчёта Risk/Reward."""
        from analysis.signal_scorer import SignalScorer
        
        entry_price = 100
        stop_loss = 95
        tp_price = 105
        
        risk_reward = SignalScorer.calculate_risk_reward(entry_price, stop_loss, tp_price)
        
        # R/R должен быть положительным
        self.assertGreater(risk_reward, 0)
        # R/R = (105-100)/(100-95) = 1
        self.assertAlmostEqual(risk_reward, 1.0, places=1)


if __name__ == '__main__':
    unittest.main()
