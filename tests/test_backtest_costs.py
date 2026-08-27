import unittest

from backtest.backtest_metrics import BacktestMetrics
from backtest.fee_calculator import FeeCalculator
from backtest.trade_simulator import TradeSimulator


class BacktestCostTests(unittest.TestCase):
    def test_round_trip_fee_charges_entry_and_exit(self):
        calculator = FeeCalculator(fee_percent=0.04)
        self.assertAlmostEqual(
            calculator.calculate_round_trip(100, 110),
            0.084,
        )

    def test_slippage_and_funding_are_recorded(self):
        simulator = TradeSimulator(
            slippage_percent=0.1,
            funding_percent=0.01,
        )
        trade = simulator.simulate(
            symbol="BTCUSDT",
            side="LONG",
            entry_candle={"close": 100},
            future_candles=[{"high": 102, "low": 100, "close": 101}],
        )
        self.assertEqual(trade["slippage_percent"], 0.1)
        self.assertGreater(trade["funding"], 0)

    def test_metrics_include_risk_adjusted_values(self):
        result = BacktestMetrics().calculate(
            [
                {"profit": 10, "profit_percent": 1},
                {"profit": -5, "profit_percent": -0.5},
                {"profit": 8, "profit_percent": 0.8},
            ]
        )
        self.assertIn("expectancy", result)
        self.assertIn("sharpe", result)
        self.assertIn("sortino", result)
