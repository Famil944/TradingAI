import unittest

from services.otc_confirmations import build
from services.otc_regime import classify, context, describe
from services.otc_strategy import Candle
from tests.test_otc_confirmations import history


class RegimeTests(unittest.TestCase):
    def test_context_ignores_future_and_detects_exit_return_and_alternation(self):
        bars = [Candle(i * 60, 100, 102, 98, 101 if i % 2 else 99) for i in range(21)]
        result = context(bars, 60, 1260)
        self.assertTrue(result["available"])
        self.assertTrue(result["alternating"])
        self.assertEqual(
            result, context(bars + [Candle(1260, 100, 200, 1, 180)], 60, 1260)
        )
        returned = bars[:-1] + [Candle(1200, 100, 104, 99, 101)]
        self.assertEqual(context(returned, 60, 1260)["event"], "выход вверх и возврат")
        breakout = bars[:-1] + [Candle(1200, 100, 104, 99, 103)]
        self.assertEqual(
            context(breakout, 60, 1260)["event"], "выход вверх из диапазона"
        )
        self.assertFalse(context(bars[:10] + bars[11:], 60, 1260)["available"])
        self.assertFalse(context(bars, 60, 1320)["available"])

    def test_descriptive_states_and_flags(self):
        features = dict(
            adx14=0.3, atr14=0.01, ema_spread=0.005, return1=-0.001, range_atr=3
        )
        state = classify(features)
        self.assertEqual(state["state"], "uptrend")
        self.assertTrue(state["pullback"])
        self.assertTrue(state["volatility_spike"])
        self.assertIn("возможный откат", describe(state))
        self.assertEqual(
            classify(dict(features, ema_spread=-0.005))["state"], "downtrend"
        )
        self.assertEqual(classify(dict(features, adx14=0.1))["state"], "range")
        self.assertEqual(classify(dict(features, adx14=0.21))["state"], "transition")
        self.assertEqual(
            classify(dict(features, adx14=float("nan")))["state"], "unknown"
        )

    def test_market_state_uses_only_closed_candles_and_is_not_a_vote(self):
        bars = history()
        now = bars[-1].time + 60
        before = build(bars, "UP", 60, now)
        future = Candle(now, 1, 10, 0.1, 9)
        self.assertEqual(before, build(bars + [future], "UP", 60, now))
        self.assertEqual(before["total"], 5)
        self.assertEqual(before["count"], len(before["matched"]))
        self.assertIn("market_state", before)
