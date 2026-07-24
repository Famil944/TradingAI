import unittest

from exchange.binance_futures_client import BinanceFuturesClient


class FakeBinanceApi:
    def __init__(self):
        self.orders = []
        self.cancelled = []
        self.fail_take_profit = False

    def exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "tickSize": "0.10",
                        },
                        {
                            "filterType": "MARKET_LOT_SIZE",
                            "stepSize": "0.001",
                            "minQty": "0.001",
                            "maxQty": "100",
                        },
                        {
                            "filterType": "MIN_NOTIONAL",
                            "notional": "5",
                        },
                    ],
                }
            ]
        }

    def ticker_price(self, symbol):
        return {"price": "50000"}

    def new_order(self, **kwargs):
        if kwargs["type"] == "TAKE_PROFIT_MARKET" and self.fail_take_profit:
            raise RuntimeError("TP rejected")
        result = {"orderId": len(self.orders) + 1, **kwargs}
        self.orders.append(result)
        return result

    def cancel_order(self, symbol, orderId):
        self.cancelled.append(orderId)
        return {"orderId": orderId}


class BinanceClientTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeBinanceApi()
        self.client = BinanceFuturesClient(self.api, lambda: None)

    def test_market_order_uses_exchange_step_size(self):
        order = self.client.market_order("BTCUSDT", "BUY", 0.0019)
        self.assertEqual(order["quantity"], "0.001")

    def test_protective_prices_use_tick_size(self):
        result = self.client.place_protective_orders(
            "BTCUSDT", "LONG", 0.0019, 49000.09, 51000.09
        )
        self.assertEqual(
            result["stop_order"]["stopPrice"],
            "49000",
        )
        self.assertEqual(
            result["take_profit_order"]["stopPrice"],
            "51000",
        )

    def test_failed_take_profit_cancels_stop(self):
        self.api.fail_take_profit = True
        with self.assertRaises(RuntimeError):
            self.client.place_protective_orders(
                "BTCUSDT", "LONG", 0.001, 49000, 51000
            )
        self.assertEqual(self.api.cancelled, [1])
