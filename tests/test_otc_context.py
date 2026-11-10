"""Observed context selects different colour hypotheses; no future prediction claims."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database.db import Database
from services.otc_conditional import Setup, context_setup, make_plan
from services.otc_ml.features import FEATURE_NAMES
from services.otc_strategy import Candle
from tests.test_otc_conditional import impulse_history


class ContextTests(unittest.TestCase):
    def test_up_momentum_chooses_green_and_mirrored_down_chooses_red(self):
        bars = impulse_history()
        at = bars[-1].time + 60
        up = context_setup(bars, at)
        self.assertEqual((up.direction, up.colour), ("UP", "green"))
        self.assertIn(":momentum:", up.rule)
        downbars = [
            Candle(b.time, 300 - b.open, 300 - b.low, 300 - b.high, 300 - b.close)
            for b in bars
        ]
        down = context_setup(downbars, at)
        self.assertEqual((down.direction, down.colour), ("DOWN", "red"))
        self.assertIn(":momentum:", down.rule)

    def test_pullback_opposite_colour_rules_are_separate_from_momentum(self):
        at = 1800000000
        for direction, body, expected in [("UP", -0.1, "red"), ("DOWN", 0.1, "green")]:
            bars = [Candle(at + i * 60, 10, 10.2, 9.8, 10) for i in range(250)]
            last = bars[-1]
            bars[-1] = Candle(last.time, 10, 10.2, 9.8, 10 + body)
            f = {key: 0.0 for key in FEATURE_NAMES}
            f.update(atr14=0.1, rsi14=0.5, adx14=0.1, ema20_gap=0.01)
            with (
                patch(
                    "services.otc_conditional.setup",
                    return_value=Setup(
                        direction, expected, 9 if direction == "UP" else 11, "fixture"
                    ),
                ),
                patch(
                    "services.otc_ml.features.indicators",
                    return_value=[f[k] for k in FEATURE_NAMES],
                ),
            ):
                result = context_setup(bars, bars[-1].time + 60)
            self.assertEqual(result.colour, expected)
            self.assertIn(":pullback:", result.rule)

    def test_future_candle_does_not_change_current_rule_or_evidence(self):
        bars = impulse_history()
        at = bars[-1].time + 60
        before = context_setup(bars, at)
        future = [Candle(at + i * 60, 1, 1e8, 0.1, 1e7) for i in range(20)]
        self.assertEqual(before, context_setup(bars + future, at))
        self.assertFalse(before.evidence["advantage_confirmed"])
        self.assertLessEqual(before.evidence["last_closed_candle"]["time"] + 60, at)

    def test_weak_or_ambiguous_context_is_skipped(self):
        from tests.test_otc_test_mode import history

        bars = history()
        self.assertIsNone(context_setup(bars, bars[-1].time + 60))

    def test_context_and_rule_identity_are_audited_without_replacing_old_stats(self):
        bars = impulse_history()
        at = bars[-1].time + 60
        context = context_setup(bars, at)
        plan = make_plan("A", at, context, at)
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "test.db")
            db.init_db()
            identifier = db.prepare_plan(plan, at, strategy=context.rule)
            with db.connect() as c:
                saved = c.execute(
                    "SELECT * FROM otc_plan_context WHERE plan_id=?", (identifier,)
                ).fetchone()
                self.assertEqual(saved["source"], "otcharts")
                payload = json.loads(saved["context"])
                self.assertEqual(payload["known_asof"], at)
                self.assertIn("macd_hist", payload["features"])
                self.assertFalse(payload["advantage_confirmed"])
            stat = db.virtual_stats()[0]
            self.assertEqual(stat["direction"], "UP")
            self.assertEqual(stat["colour"], "green")
            self.assertTrue(stat["strategy"].startswith("conditional-context-v2"))


if __name__ == "__main__":
    unittest.main()
