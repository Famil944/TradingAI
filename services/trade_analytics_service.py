from services.demo_statistics_service import DemoStatisticsService


class TradeAnalyticsService:

    def __init__(self, statistics=None):
        self.statistics = statistics or DemoStatisticsService()

    def summary(self, trading_mode=None):
        stats = self.statistics.get_statistics(
            trading_mode=trading_mode,
        )
        return {
            "count": stats["closed_trades"],
            "total_profit": stats["total_pnl"],
            "average_profit": stats["average_pnl"],
            "best_trade": stats["best_trade"],
            "worst_trade": stats["worst_trade"],
        }
