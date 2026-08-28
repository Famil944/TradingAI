import unittest
from unittest.mock import patch

from config.models import CandleData
from services.scanner import MarketScanner
from bot.commands import _price


def candles(count):
    return [
        CandleData(
            timestamp=index,
            open=100 + index * 0.05,
            high=101 + index * 0.05,
            low=99 + index * 0.05,
            close=100.5 + index * 0.05,
            volume=1_000_000,
        )
        for index in range(count)
    ]


class FakeClient:
    def __init__(self, daily_count, quote_volume=10_000_000, bid=99.95, ask=100.05):
        self.daily_count = daily_count
        self.quote_volume = quote_volume
        self.bid = bid
        self.ask = ask

    async def get_klines(self, symbol, interval, limit=100):
        if interval == "1d":
            return candles(self.daily_count)
        return candles(limit)

    async def get_ticker(self, symbol):
        return {
            "price": 100,
            "price_change": -5,
            "price_change_percent": -5,
            "quote_asset_volume": self.quote_volume,
            "bid_price": self.bid,
            "ask_price": self.ask,
        }

    async def get_tick_size(self, symbol):
        return 0.00001


class SpotScannerSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_price_uses_binance_tick_size_precision(self):
        self.assertEqual(_price(0.006965, 0.00001), "0.00696")
        self.assertEqual(_price(0.007021, 0.00001), "0.00702")

    def test_relative_volume_uses_three_closed_candles(self):
        series = candles(20)
        series.extend([
            CandleData(timestamp=21 + index, open=1, high=1, low=1, close=1, volume=800_000)
            for index in range(3)
        ])
        self.assertAlmostEqual(MarketScanner._relative_volume_ratio(series), 0.8)

    async def test_open_candle_is_removed_from_every_timeframe(self):
        scanner = MarketScanner()
        with patch(
            "services.scanner.SignalScorer.generate_signal", return_value=None
        ) as generate:
            await scanner._analyze_symbol(FakeClient(daily_count=61), "BTCUSDT")

        args = generate.call_args.args
        self.assertEqual(len(args[2]), 120)
        self.assertEqual(len(generate.call_args.kwargs["candles_15m"]), 120)
        self.assertEqual(len(generate.call_args.kwargs["candles_1h"]), 120)
        self.assertEqual(len(generate.call_args.kwargs["candles_4h"]), 120)

    async def test_new_coin_is_rejected_before_scoring(self):
        scanner = MarketScanner()
        with patch("services.scanner.SignalScorer.generate_signal") as generate:
            result = await scanner._analyze_symbol(
                FakeClient(daily_count=30), "NEWUSDT"
            )

        self.assertIsNone(result)
        generate.assert_not_called()

    async def test_low_liquidity_is_rejected_before_scoring(self):
        scanner = MarketScanner()
        with patch("services.scanner.SignalScorer.generate_signal") as generate:
            result = await scanner._analyze_symbol(
                FakeClient(daily_count=61, quote_volume=9_999_999), "LOWUSDT"
            )
        self.assertIsNone(result)
        self.assertEqual(scanner._symbol_diagnostics["LOWUSDT"]["reason"], "low_liquidity")
        generate.assert_not_called()

    async def test_wide_spread_is_rejected_before_scoring(self):
        scanner = MarketScanner()
        with patch("services.scanner.SignalScorer.generate_signal") as generate:
            result = await scanner._analyze_symbol(
                FakeClient(daily_count=61, bid=99.0, ask=101.0), "WIDEUSDT"
            )
        self.assertIsNone(result)
        self.assertEqual(scanner._symbol_diagnostics["WIDEUSDT"]["reason"], "wide_spread")
        generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
