import unittest
from types import SimpleNamespace
from services.otc_policy import evaluate


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.bars = [
            SimpleNamespace(open=1, close=1.01, high=1.02, low=0.99)
            for _ in range(22)
        ]
        self.features = {"atr14": 0.02, "rsi14": 0.5}

    def test_doji_is_rejected(self):
        bars = list(self.bars)
        bars[-1] = SimpleNamespace(open=1, close=1.0001, high=1.02, low=0.98)
        result = evaluate(bars, self.features, "UP", {"state": "uptrend"})
        self.assertFalse(result["gates"]["not_doji"])

    def test_transition_is_not_traded(self):
        result = evaluate(self.bars, self.features, "UP", {"state": "transition"})
        self.assertEqual(result["reason"], "market_transition_or_unknown")

    def test_opposite_trend_rejected(self):
        result = evaluate(self.bars, self.features, "DOWN", {"state": "uptrend"})
        self.assertFalse(result["gates"]["trend_aligned"])

    def test_missing_context_is_blocked(self):
        self.assertEqual(
            evaluate(self.bars[:5], self.features, "UP", {"state": "uptrend"})["reason"],
            "insufficient_price_context",
        )

    def test_upper_shadow_prevents_up_trade(self):
        bars = list(self.bars)
        bars[-1] = SimpleNamespace(open=1, close=1.005, high=1.1, low=0.999)
        result = evaluate(bars, self.features, "UP", {"state": "uptrend"})
        self.assertFalse(result["gates"]["no_opposing_wick"])
