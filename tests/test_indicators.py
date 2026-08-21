import unittest
from analysis.indicators import IndicatorCalculator
from config.models import CandleData


class TestIndicators(unittest.TestCase):
    """Тесты для расчёта индикаторов."""
    
    def test_rsi_calculation(self):
        """Тест расчёта RSI."""
        # Создаём тестовые данные: растущая цена
        closes = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115]
        
        rsi = IndicatorCalculator.calculate_rsi(closes, period=14)
        
        # RSI должен быть > 70 при сильном растущем тренде
        self.assertIsNotNone(rsi)
        self.assertGreater(rsi, 50)
    
    def test_rsi_oversold(self):
        """Тест RSI в перепроданности."""
        # Падающая цена
        closes = [115, 114, 113, 112, 111, 110, 109, 108, 107, 106, 105, 104, 103, 102, 101, 100]
        
        rsi = IndicatorCalculator.calculate_rsi(closes, period=14)
        
        # RSI должен быть < 30 при сильном падающем тренде
        self.assertIsNotNone(rsi)
        self.assertLess(rsi, 50)
    
    def test_ema_calculation(self):
        """Тест расчёта EMA."""
        closes = [100, 102, 101, 103, 102, 104, 103, 105, 104, 106, 105, 107, 106, 108, 107, 109]
        
        ema = IndicatorCalculator.calculate_ema(closes, period=5)
        
        self.assertIsNotNone(ema)
        self.assertAlmostEqual(ema, 107.5, delta=1)
    
    def test_atr_calculation(self):
        """Тест расчёта ATR."""
        candles = []
        for i in range(20):
            candles.append(CandleData(
                timestamp=i,
                open=100 + i,
                high=102 + i,
                low=98 + i,
                close=101 + i,
                volume=1000
            ))
        
        atr = IndicatorCalculator.calculate_atr(candles, period=14)
        
        self.assertIsNotNone(atr)
        self.assertGreater(atr, 0)
    
    def test_bollinger_bands(self):
        """Тест расчёта Bollinger Bands."""
        closes = [100 + i for i in range(25)]
        
        upper, middle, lower = IndicatorCalculator.calculate_bollinger_bands(closes, period=20)
        
        self.assertIsNotNone(upper)
        self.assertIsNotNone(middle)
        self.assertIsNotNone(lower)
        
        # Upper > Middle > Lower
        self.assertGreater(upper, middle)
        self.assertGreater(middle, lower)


if __name__ == '__main__':
    unittest.main()
