"""Offline tests; no API calls, trades or credentials."""
import unittest
from services.otc_strategy import Candle, aggregate, candidate, normalize_candles
from services.otc_session import eligible


class FakeAsset:
    def __init__(self, symbol, payout, open=True, is_otc=True):
        self.symbol = symbol
        self.payout = payout
        self.open = open
        self.is_otc = is_otc


class OTCSignalsTests(unittest.TestCase):
    def test_payout_filter_rejects_unknown_and_closed(self):
        items = [
            FakeAsset("A_otc", 92),
            FakeAsset("B_otc", 89),
            FakeAsset("C_otc", None),
            FakeAsset("D_otc", 95, open=False),
            FakeAsset("E", 91, is_otc=False),
            FakeAsset("F_otc", 90, open=None),
        ]
        self.assertEqual([v.symbol for v in eligible(items)], ["A_otc", "F_otc"])

    def test_groups_require_complete_consecutive_minutes(self):
        bars = [Candle(i * 60, 2, 3, 1, 2.5) for i in range(5)]
        self.assertEqual(len(aggregate(bars, 300)), 1)
        self.assertEqual(aggregate(bars[:-1], 300), [])
        self.assertEqual(aggregate(bars[1:], 300), [])

    def test_unclosed_or_future_bars_are_not_used(self):
        now = 1_800_000_000
        start = now // 60 * 60
        raw = [{"time": start, "open": 1, "high": 2, "low": 0.5, "close": 1.2}]
        self.assertEqual(normalize_candles(raw, now), [])

    def test_no_signal_when_insufficient_history_or_gap(self):
        data = [Candle(i * 60, 1, 1.2, .9, 1.1) for i in range(100)]
        self.assertIsNone(candidate(data, 300, 6000))
        self.assertIsNone(candidate(data[:50] + data[52:], 60, 6000))
        self.assertIsNone(candidate(data, 60, 6001))

    def test_no_signal_when_no_momentum(self):
        data = [Candle(i * 60, 1, 1.01, .99, 1) for i in range(1200)]
        self.assertIsNone(candidate(data, 60, 1200 * 60))
        self.assertIsNone(candidate(data, 300, 1200 * 60))
        self.assertIsNone(candidate(data, 900, 1200 * 60))


if __name__ == "__main__":
    unittest.main()
