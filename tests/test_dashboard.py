import unittest

from bot.dashboard import build_dashboard


class FakeEngine:
    trader = type("Trader", (), {"position": None})()

    @staticmethod
    def status():
        return {
            "enabled": False,
            "balance": 10000,
            "trades": 0,
            "winrate": 0,
        }


class FakePaper:
    engine = FakeEngine()


class FakeAutoState:
    enabled = True
    settings = type(
        "Settings",
        (),
        {"get": staticmethod(lambda key: "2026-07-30T15:00:00+00:00")},
    )()


class FakeClient:
    @staticmethod
    def open_positions():
        return [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.01",
                "unRealizedProfit": "1.25",
            }
        ]

    @staticmethod
    def account():
        return {
            "assets": [
                {
                    "asset": "USDT",
                    "availableBalance": "97.25",
                }
            ]
        }


class FakeController:
    client = FakeClient()


class FakeStatistics:
    @staticmethod
    def get_statistics(trading_mode=None):
        return {"total_trades": 5, "win_rate": 66.67}


class DashboardTests(unittest.TestCase):
    def test_exchange_account_replaces_paper_values(self):
        text = build_dashboard(
            FakePaper(),
            FakeAutoState(),
            FakeController(),
            FakeStatistics(),
        )

        self.assertIn("BTCUSDT LONG", text)
        self.assertIn("Доступно: 97.25 USDT", text)
        self.assertIn("Сделок: 5", text)
        self.assertIn("Winrate: 66.67%", text)
        self.assertIn("Текущий PnL: 1.2500 USDT", text)
        self.assertNotIn("Баланс: 10000", text)
