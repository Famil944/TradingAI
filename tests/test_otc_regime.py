import unittest

from services.otc_confirmations import build
from services.otc_regime import classify, describe
from services.otc_strategy import Candle
from tests.test_otc_confirmations import history


class RegimeTests(unittest.TestCase):
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
