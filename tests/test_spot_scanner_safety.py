import unittest
from unittest.mock import patch

from config.models import CandleData
from services.scanner import MarketScanner
from bot.commands import _price


def candles(count):
    return [
        CandleData(
            timestamp=index,
            open=100,
            high=102,
            low=99,
            close=101,
            volume=1_000_000,
        )
        for index in range(count)
    ]


class FakeClient:
    def __init__(self, daily_count):
        self.daily_count = daily_count

    async def get_klines(self, symbol, interval, limit=100):
        if interval == "1d":
            return candles(self.daily_count)
        return candles(limit)

    async def get_ticker(self, symbol):
        return {
            "price": 100,
            "price_change": -5,
            "price_change_percent": -5,
            "quote_asset_volume": 10_000_000,
        }

    async def get_tick_size(self, symbol):
        return 0.00001


class SpotScannerSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_price_uses_binance_tick_size_precision(self):
        self.assertEqual(_price(0.006965, 0.00001), "0.00696")
        self.assertEqual(_price(0.007021, 0.00001), "0.00702")

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


if __name__ == "__main__":
    unittest.main()
