import hashlib
import hmac
import unittest
from urllib.parse import urlencode

from exchange.portfolio_margin_api import PortfolioMarginApi


class FakeResponse:
    def __init__(self, data, status_code=200):
        self.data = data
        self.status_code = status_code
        self.headers = {}
        self.text = str(data)

    def json(self):
        return self.data


class FakeSession:
    def __init__(self):
        self.calls = []
        self.responses = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class FakePublic:
    def ping(self):
        return {}

    def time(self):
        return {"serverTime": 1}


class PortfolioMarginApiTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.api = PortfolioMarginApi(
            "api-key",
            "secret",
            FakePublic(),
            session=self.session,
        )

    def test_signed_request_uses_hmac_and_papi(self):
        self.session.responses.append(FakeResponse({"ok": True}))
        self.api._signed_request(
            "GET", "/papi/v1/um/account", symbol="BTCUSDT"
        )
        method, url, kwargs = self.session.calls[0]
        params = dict(kwargs["params"])
        signature = params.pop("signature")
        expected = hmac.new(
            b"secret",
            urlencode(params).encode(),
            hashlib.sha256,
        ).hexdigest()

        self.assertEqual(method, "GET")
        self.assertEqual(
            url,
            "https://papi.binance.com/papi/v1/um/account",
        )
        self.assertEqual(signature, expected)
        self.assertEqual(kwargs["headers"]["X-MBX-APIKEY"], "api-key")

    def test_market_and_conditional_orders_use_different_routes(self):
        self.session.responses.extend(
            [
                FakeResponse({"orderId": 1}),
                FakeResponse(
                    {
                        "strategyId": 2,
                        "strategyStatus": "NEW",
                        "strategyType": "STOP_MARKET",
                    }
                ),
            ]
        )
        self.api.new_order(
            "BTCUSDT", "BUY", "MARKET", quantity="0.01"
        )
        stop = self.api.new_order(
            "BTCUSDT",
            "SELL",
            "STOP_MARKET",
            quantity="0.01",
            stopPrice="90000",
        )

        self.assertTrue(
            self.session.calls[0][1].endswith("/papi/v1/um/order")
        )
        self.assertTrue(
            self.session.calls[1][1].endswith(
                "/papi/v1/um/conditional/order"
            )
        )
        self.assertEqual(stop["orderId"], 2)
        self.assertTrue(stop["_conditional"])

    def test_account_exposes_available_balance(self):
        self.session.responses.extend(
            [
                FakeResponse(
                    {
                        "assets": [
                            {
                                "asset": "USDT",
                                "crossWalletBalance": "100",
                            }
                        ]
                    }
                ),
                FakeResponse(
                    {
                        "totalAvailableBalance": "80",
                        "accountStatus": "NORMAL",
                    }
                ),
            ]
        )
        account = self.api.account()
        self.assertEqual(
            account["assets"][0]["availableBalance"], "80"
        )
        self.assertTrue(account["canTrade"])
