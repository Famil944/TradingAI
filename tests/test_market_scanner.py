import unittest

from scanner.market_scanner import MarketScanner


class FakeMarket:
    @staticmethod
    def get_top_usdt_symbols(limit):
        return ["XAUUSDT", "BTCUSDT", "SPCXUSDT", "ETHUSDT"]


class FakeFearGreed:
    @staticmethod
    def get_index():
        return {}


class FakeCore:
    market = FakeMarket()
    fear_greed = FakeFearGreed()

    @staticmethod
    def analyze_symbol(symbol, interval, fear_greed_data):
        return {"symbol": symbol, "score": 1}


class MarketScannerTests(unittest.TestCase):
    def test_only_conservative_crypto_allowlist_is_analyzed(self):
        results = MarketScanner(FakeCore()).scan_market()
        self.assertEqual(
            [item["symbol"] for item in results],
            ["BTCUSDT", "ETHUSDT"],
        )
