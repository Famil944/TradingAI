import unittest

from exchange.usdm_futures_api import UsdmFuturesApi


class FakeClient:
    def __init__(self):
        self.calls = []

    def new_order(self, **kwargs):
        self.calls.append(("new_order", kwargs))
        return {"orderId": 1}

    def sign_request(self, method, path, payload):
        self.calls.append((method, path, payload))
        if method == "POST":
            return {
                "algoId": 42,
                "algoStatus": "NEW",
                "orderType": payload["type"],
                "triggerPrice": payload["triggerPrice"],
            }
        if path.endswith("openAlgoOrders"):
            return []
        return {"algoId": payload.get("algoId"), "algoStatus": "NEW"}

    def get_orders(self, **kwargs):
        self.calls.append(("get_orders", kwargs))
        return []

    def cancel_open_orders(self, symbol):
        return {"code": 200}


class UsdmFuturesApiTests(unittest.TestCase):
    def setUp(self):
        self.raw = FakeClient()
        self.api = UsdmFuturesApi(self.raw)

    def test_market_order_uses_regular_endpoint(self):
        result = self.api.new_order(
            symbol="BTCUSDT",
            side="BUY",
            type="MARKET",
            quantity="0.001",
        )
        self.assertEqual(result["orderId"], 1)
        self.assertEqual(self.raw.calls[0][0], "new_order")

    def test_conditional_order_uses_algo_endpoint(self):
        result = self.api.new_order(
            symbol="BTCUSDT",
            side="SELL",
            type="STOP_MARKET",
            quantity="0.001",
            stopPrice="60000",
            reduceOnly="true",
        )
        method, path, payload = self.raw.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/fapi/v1/algoOrder")
        self.assertEqual(payload["algoType"], "CONDITIONAL")
        self.assertEqual(payload["triggerPrice"], "60000")
        self.assertNotIn("stopPrice", payload)
        self.assertEqual(result["orderId"], 42)
        self.assertEqual(result["stopPrice"], "60000")

    def test_known_algo_order_uses_algo_cancel_endpoint(self):
        self.api.new_order(
            symbol="BTCUSDT",
            side="SELL",
            type="TAKE_PROFIT_MARKET",
            quantity="0.001",
            stopPrice="70000",
        )
        self.api.cancel_order("BTCUSDT", 42)
        self.assertEqual(
            self.raw.calls[-1][1],
            "/fapi/v1/algoOrder",
        )

    def test_open_orders_uses_plural_sdk_endpoint(self):
        result = self.api.get_open_orders("BTCUSDT")
        self.assertEqual(result, [])
        self.assertEqual(
            self.raw.calls[0],
            ("get_orders", {"symbol": "BTCUSDT"}),
        )
        self.assertEqual(
            self.raw.calls[1][1],
            "/fapi/v1/openAlgoOrders",
        )
