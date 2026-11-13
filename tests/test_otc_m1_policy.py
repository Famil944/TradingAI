import unittest

from services.otc_m1_policy import evaluate, live_check
from services.otc_strategy import Candle


class M1StrictPolicyTests(unittest.TestCase):
    def setUp(self):
        self.start = 1800000000
        self.bars = []
        for i in range(195):
            value = 1 + 0.00005 * i
            self.bars.append(
                Candle(self.start + i * 60, value, value + 0.00012,
                       value - 0.00012, value + 0.00003)
            )
        self.asof = self.bars[-1].time + 60
        self.now = self.asof + 42
        self.features = {"atr14": 0.0004}
        self.state = {"state": "uptrend"}
        self.tick = (self.bars[-1].close + 0.00001, self.now - 0.4, self.now - 0.2)

    def test_closed_m5_trend_can_pass_up(self):
        result = evaluate(self.bars, self.features, "UP", self.state,
                          self.asof, self.tick, self.now)
        self.assertTrue(result["passed"], result)
        self.assertLessEqual(result["metrics"]["m5_closed_at"], self.asof)

    def test_no_future_m1_or_unfinished_m5_leaks(self):
        baseline = evaluate(self.bars, self.features, "UP", self.state,
                            self.asof, self.tick, self.now)
        altered = self.bars + [Candle(self.asof, 100, 200, 50, 150)]
        self.assertEqual(
            evaluate(altered, self.features, "UP", self.state,
                     self.asof, self.tick, self.now), baseline
        )

    def test_rejects_unaligned_m5_and_flat_m1(self):
        down = evaluate(self.bars, self.features, "DOWN",
                        {"state": "downtrend"}, self.asof, self.tick, self.now)
        self.assertFalse(down["gates"]["closed_m5_trend"])
        flat = evaluate(self.bars, self.features, "UP",
                        {"state": "range"}, self.asof, self.tick, self.now)
        self.assertFalse(flat["gates"]["m1_trend"])

    def test_stale_and_adverse_ticks_block(self):
        stale = (self.tick[0], self.now - 9, self.now - 9)
        self.assertEqual(live_check(self.bars[-1].close, 0.0004, "UP",
                                    stale, self.now)["reason"], "m1_stale_tick")
        adverse = (self.bars[-1].close - 0.0002, self.now - 0.1, self.now)
        self.assertEqual(live_check(self.bars[-1].close, 0.0004, "UP",
                                    adverse, self.now)["reason"], "m1_adverse_live_move")
        spike = (self.bars[-1].close + 0.0004, self.now - 0.1, self.now)
        self.assertEqual(live_check(self.bars[-1].close, 0.0004, "UP",
                                    spike, self.now)["reason"], "m1_preentry_overextension")

    def test_missing_m5_history_blocks(self):
        row = evaluate(self.bars[:40], self.features, "UP", self.state,
                       self.asof, self.tick, self.now)
        self.assertFalse(row["passed"])

    def test_gap_blocks(self):
        bars = self.bars.copy()
        del bars[-70]
        result = evaluate(bars, self.features, "UP", self.state,
                          self.asof, self.tick, self.now)
        self.assertFalse(result["passed"])
