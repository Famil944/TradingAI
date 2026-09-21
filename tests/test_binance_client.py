import unittest
from unittest.mock import AsyncMock

from exchange.binance_client import BinanceClient


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self.payload

    async def text(self):
        return "error"


class FakeSession:
    def __init__(self):
        self.urls = []

    def request(self, method, url, params=None, timeout=None):
        self.urls.append(url)
        if url.startswith("https://blocked.example"):
            return FakeResponse(451)
        return FakeResponse(200, {"ok": True})


class BinanceClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        BinanceClient._blocked_base_urls = set()
        BinanceClient._preferred_base_url = None
        BinanceClient._unavailable_until = {}
        BinanceClient._last_all_unavailable_log = 0.0
        BinanceClient._exchange_info_cache = None
        BinanceClient._exchange_info_cached_at = 0.0

    async def test_451_endpoint_is_not_requested_again(self):
        client = BinanceClient()
        client.BASE_URL = "https://blocked.example"
        client.PUBLIC_BASE_URLS = ("https://working.example",)
        client.session = FakeSession()

        self.assertEqual(await client._request("GET", "/test"), {"ok": True})
        self.assertEqual(await client._request("GET", "/next"), {"ok": True})

        blocked_calls = [url for url in client.session.urls if "blocked.example" in url]
        working_calls = [url for url in client.session.urls if "working.example" in url]
        self.assertEqual(len(blocked_calls), 1)
        self.assertEqual(len(working_calls), 2)

    async def test_quote_like_assets_are_excluded_from_top(self):
        client = BinanceClient()

        async def fake_request(*args, **kwargs):
            return [
                {"symbol": "BTCUSDT", "quoteVolume": "100"},
                {"symbol": "FDUSDUSDT", "quoteVolume": "1000"},
                {"symbol": "RLUSDUSDT", "quoteVolume": "900"},
                {"symbol": "EURUSDT", "quoteVolume": "800"},
                {"symbol": "SOLUSDT", "quoteVolume": "90"},
            ]

        client._request = fake_request
        client.get_spot_symbol_map = AsyncMock(return_value={})
        self.assertEqual(await client.get_top_symbols(10), ["BTCUSDT", "SOLUSDT"])

    async def test_unavailable_preferred_endpoint_falls_back(self):
        client = BinanceClient()
        client.BASE_URL = "https://failed.example"
        client.PUBLIC_BASE_URLS = ("https://working.example",)
        client.session = FakeSession()
        BinanceClient._preferred_base_url = "https://failed.example"

        original_request = client._request_from_base

        async def fake_request(method, base_url, endpoint, params):
            if base_url == "https://failed.example":
                return "unavailable", None
            return await original_request(method, base_url, endpoint, params)

        client._request_from_base = fake_request
        self.assertEqual(await client._request("GET", "/test"), {"ok": True})
        self.assertEqual(
            BinanceClient._preferred_base_url, "https://working.example"
        )


if __name__ == "__main__":
    unittest.main()
