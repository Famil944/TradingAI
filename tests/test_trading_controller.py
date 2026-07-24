import unittest
from unittest.mock import patch

from services.demo_trading_controller import DemoTradingController


class FakeClient:
    def __init__(self, fail_protection=False):
        self.fail_protection = fail_protection
        self.closed = []
        self.cancelled = []

    def open_positions(self, symbol=None):
        return []

    def price(self, symbol):
        return {"price": "100"}

    def klines(self, symbol, interval, limit):
        return []

    def account(self):
        return {
            "assets": [
                {"asset": "USDT", "availableBalance": "1000"}
            ]
        }

    def market_order(self, symbol, side, quantity):
        return {
            "orderId": 1,
            "executedQty": str(quantity),
            "avgPrice": "100",
        }

    def place_protective_orders(self, **kwargs):
        if self.fail_protection:
            raise RuntimeError("protection rejected")
        return {
            "stop_order": {"orderId": 2},
            "take_profit_order": {"orderId": 3},
        }

    def cancel_all_orders(self, symbol):
        self.cancelled.append(symbol)

    def close_position_market(self, **kwargs):
        self.closed.append(kwargs)
        return {"orderId": 4}


class FakeTradeLog:
    def __init__(self):
        self.saved = []

    def get_recent_trades(self, limit=100):
        return []

    def save_open_trade(self, **kwargs):
        self.saved.append(kwargs)


class FakeAnalyzer:
    def analyze(self, klines):
        return {"atr": 2}


class FakeThread:
    def __init__(self, target, daemon):
        self.target = target

    def start(self):
        return None


class TradingControllerTests(unittest.TestCase):
    @patch(
        "services.demo_trading_controller.threading.Thread",
        FakeThread,
    )
    def test_open_trade_stores_exchange_order_ids(self):
        client = FakeClient()
        trade_log = FakeTradeLog()
        controller = DemoTradingController(
            client=client,
            trade_log=trade_log,
            analyzer=FakeAnalyzer(),
        )
        result = controller.open_demo_trade(
            {"symbol": "BTCUSDT", "signal": "🟢 LONG"}
        )

        self.assertIn("сделка открыта", result)
        self.assertEqual(trade_log.saved[0]["entry_order_id"], 1)
        self.assertEqual(trade_log.saved[0]["stop_order_id"], 2)
        self.assertEqual(
            trade_log.saved[0]["take_profit_order_id"], 3
        )

    def test_protection_failure_closes_new_position(self):
        client = FakeClient(fail_protection=True)
        controller = DemoTradingController(
            client=client,
            trade_log=FakeTradeLog(),
            analyzer=FakeAnalyzer(),
        )
        with self.assertRaises(RuntimeError):
            controller.open_demo_trade(
                {"symbol": "BTCUSDT", "signal": "🟢 LONG"}
            )

        self.assertEqual(client.cancelled, ["BTCUSDT"])
        self.assertEqual(client.closed[0]["side"], "SELL")
