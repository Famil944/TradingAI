import unittest

from services.trade_analytics_service import TradeAnalyticsService


class FakeStatistics:
    def __init__(self):
        self.mode = None

    def get_statistics(self, trading_mode=None):
        self.mode = trading_mode
        return {
            "closed_trades": 3,
            "total_pnl": 15.2896,
            "average_pnl": 5.0965,
            "best_trade": 20.212,
            "worst_trade": -9.2389,
        }


class TradeAnalyticsTests(unittest.TestCase):
    def test_uses_exchange_trade_statistics_for_active_mode(self):
        statistics = FakeStatistics()
        result = TradeAnalyticsService(statistics).summary("DEMO")

        self.assertEqual(statistics.mode, "DEMO")
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["total_profit"], 15.2896)
        self.assertEqual(result["worst_trade"], -9.2389)
