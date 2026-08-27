import unittest

from auto.candidate_selector import CandidateSelector
from auto.multi_tf_filter import MultiTimeframeFilter
from strategy.momentum import Momentum
from strategy.strategy_engine import StrategyEngine


def analysis(signal, score, rsi, macd, macd_signal):
    return {
        "signal": signal,
        "score": score,
        "rsi": rsi,
        "macd": macd,
        "macd_signal": macd_signal,
        "ema20": 110 if signal == "🟢 LONG" else 90,
        "ema50": 100,
        "location": "MIDDLE",
    }


class FakeMultiAnalyzer:
    def __init__(self, results):
        self.results = results

    def analyze(self, symbol):
        scores = [item["score"] for item in self.results]
        return {
            "symbol": symbol,
            "final_signal": "🟡 WAIT",
            "avg_score": sum(scores) / len(scores),
            "results": self.results,
        }


class AutoFilterTests(unittest.TestCase):
    def test_short_signal_uses_negative_score_magnitude(self):
        item = analysis("🔴 SHORT", -50, 45, -2, -1)
        self.assertTrue(StrategyEngine().validate_short(item)["allowed"])

    def test_long_signal_threshold_matches_signal_engine(self):
        item = analysis("🟢 LONG", 45, 55, 2, 1)
        self.assertTrue(StrategyEngine().validate_long(item)["allowed"])

    def test_short_momentum_is_direction_aware(self):
        item = analysis("🔴 SHORT", -50, 45, -2, -1)
        self.assertGreater(Momentum().analyze(item)["score"], 0)

    def test_candidates_rank_by_absolute_directional_strength(self):
        candidates = CandidateSelector().select_candidates([
            {"symbol": "LONG", "signal": "🟢 LONG", "score": 45},
            {"symbol": "SHORT", "signal": "🔴 SHORT", "score": -60},
        ])
        self.assertEqual(candidates[0]["symbol"], "SHORT")

    def test_balanced_multi_tf_accepts_one_match_with_directional_consensus(self):
        results = [
            {"signal": "🔴 SHORT", "score": -50},
            {"signal": "🟡 WAIT", "score": -30},
            {"signal": "🟡 WAIT", "score": -20},
        ]
        check = MultiTimeframeFilter(
            FakeMultiAnalyzer(results)
        ).check("BTCUSDT", "SHORT")
        self.assertTrue(check["approved"])

    def test_multi_tf_rejects_opposite_aggregate(self):
        results = [
            {"signal": "🔴 SHORT", "score": -45},
            {"signal": "🟢 LONG", "score": 70},
            {"signal": "🟢 LONG", "score": 60},
        ]
        check = MultiTimeframeFilter(
            FakeMultiAnalyzer(results)
        ).check("BTCUSDT", "SHORT")
        self.assertFalse(check["approved"])


if __name__ == "__main__":
    unittest.main()
