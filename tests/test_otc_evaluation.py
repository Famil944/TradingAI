"""Do not count an UP move as a win when the required red candle was green."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from database.db import Database
from services.otc_conditional import Setup, make_plan
from services.otc_evaluation import Evaluator, assess, format_stats
from services.otc_strategy import Candle


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.db")
        self.db.init_db()
        self.at = 1800000000
        self.plan = make_plan("A", self.at, Setup("UP", "red", 0.5, "fixture"), self.at)
        self.id = self.db.prepare_plan(self.plan, self.at)
        self.db.plan_delivered(self.id, self.at + 1, True)
        self.now = self.at + 305
        self.row = self.db.assessment_targets(self.now)[0]

    def tearDown(self):
        self.temp.cleanup()

    def bars(self, colour="red", entry=1, expiry=1.1, close=None):
        c = (0.9 if colour == "red" else 1.1) if close is None else close
        return [
            Candle(self.at + 120, 1, max(1, c) + 0.1, min(1, c) - 0.1, c),
            Candle(self.at + 180, entry, entry + 0.1, entry - 0.1, entry),
            Candle(self.at + 240, expiry, expiry + 0.1, expiry - 0.1, expiry),
        ]

    def test_green_condition_then_price_up_is_skip_not_win(self):
        result = assess(self.row, self.bars("green", expiry=1.5), self.now)
        self.assertEqual(result["outcome"], "SKIPPED")
        self.assertEqual(result["condition"], "NOT_MET")

    def test_matching_red_condition_then_up_down_equal(self):
        for price, outcome in ((1.1, "WIN"), (0.9, "LOSS"), (1, "DRAW")):
            self.assertEqual(
                assess(self.row, self.bars(expiry=price), self.now)["outcome"], outcome
            )

    def test_doji_or_invalidation_level_is_skip_even_if_direction_wins(self):
        for close in (1, 0.5, 0.4):
            self.assertEqual(
                assess(self.row, self.bars(close=close), self.now)["outcome"], "SKIPPED"
            )

    def test_missing_expiry_is_unknown_but_false_condition_still_skip(self):
        self.assertEqual(
            assess(self.row, self.bars()[:-1], self.now)["outcome"], "UNKNOWN"
        )
        self.assertEqual(
            assess(self.row, self.bars("green")[:1], self.now)["outcome"], "SKIPPED"
        )
        self.assertEqual(
            assess(self.row, self.bars()[1:], self.now)["outcome"], "UNKNOWN"
        )

    def test_no_early_future_endpoint_or_unconfirmed_delivery(self):
        self.assertEqual(
            assess(self.row, self.bars(), self.at + 240)["outcome"], "UNKNOWN"
        )
        for delivery in ("PENDING", "LATE", "FAILED"):
            result = assess({**self.row, "delivery": delivery}, self.bars(), self.now)
            self.assertNotEqual(result["outcome"], "WIN")

    def test_down_green_and_observer_green_up_are_evaluated_by_actual_rule(self):
        down = {**self.row, "direction": "DOWN", "colour": "green", "invalidation": 2}
        self.assertEqual(
            assess(down, self.bars("green", expiry=0.8), self.now)["outcome"], "WIN"
        )
        observer = {**self.row, "colour": "green"}
        self.assertEqual(
            assess(observer, self.bars("green", expiry=1.2), self.now)["outcome"], "WIN"
        )

    def test_cancel_before_entry_excluded_after_entry_still_evaluated(self):
        self.db.close_plans(self.at + 181, cancel=True)
        with self.db.connect() as c:
            self.assertEqual(
                c.execute("SELECT lifecycle FROM otc_conditional_plans").fetchone()[0],
                "ACTIVE",
            )
        self.db.close_plans(self.at + 180, cancel=True)
        self.db.close_plans(self.at + 179, cancel=True)
        with self.db.connect() as c:
            self.assertEqual(
                c.execute("SELECT lifecycle FROM otc_conditional_plans").fetchone()[0],
                "CANCELLED",
            )

    def test_once_only_persistence_does_not_rewrite_trade_journal(self):
        result = assess(self.row, self.bars(), self.now)
        self.assertTrue(self.db.assess_plan(self.id, result, self.now))
        self.assertFalse(
            self.db.assess_plan(self.id, {**result, "outcome": "LOSS"}, self.now + 1)
        )
        self.assertEqual(self.db.virtual_stats()[0]["wins"], 1)
        self.assertEqual(self.db.stats(), [])
        with self.db.connect() as c:
            self.assertEqual(
                c.execute("SELECT outcome FROM otc_conditional_plans").fetchone()[0],
                "UNKNOWN",
            )

    def test_conflicting_duplicate_candle_cannot_become_win(self):
        bars = self.bars()
        bars.append(Candle(self.at + 120, 1, 1.3, 0.8, 1.2))
        self.assertEqual(assess(self.row, bars, self.now)["outcome"], "UNKNOWN")

    def test_virtual_assessments_survive_reinitialization(self):
        result = assess(self.row, self.bars("green"), self.now)
        self.db.assess_plan(self.id, result, self.now)
        reopened = Database(self.db.db_path)
        reopened.init_db()
        self.assertEqual(reopened.virtual_stats()[0]["skipped"], 1)
        self.assertEqual(reopened.assessment_targets(self.now), [])

    def test_percent_excludes_skips_unknown_draws_and_cancelled(self):
        row = dict(
            strategy="fixture",
            wins=2,
            losses=1,
            draws=1,
            skipped=8,
            unknown=4,
            cancelled=3,
            not_delivered=2,
            pending=1,
        )
        text = format_stats([row], "test")
        self.assertIn("66.7% (n=3)", text)
        self.assertIn("Пропуск 8", text)
        self.assertIn("виртуально по OTCharts", text)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_uses_rest_and_notifies_once(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "bot.db")
            db.init_db()
            at = 1800000000
            plan = make_plan("A", at, Setup("UP", "red", 0.5, "fixture"), at)
            identifier = db.prepare_plan(plan, at)
            db.plan_delivered(identifier, at + 1, True)
            bars = [
                Candle(at + 120, 1, 1.1, 0.8, 0.9),
                Candle(at + 180, 1, 1.1, 0.9, 1),
                Candle(at + 240, 1.2, 1.3, 1.1, 1.2),
            ]
            provider = SimpleNamespace(
                usage=AsyncMock(return_value=SimpleNamespace(remaining=100)),
                candles=AsyncMock(return_value=bars),
            )
            bot = SimpleNamespace(send_message=AsyncMock())
            worker = Evaluator(db, provider, bot, 1, clock=lambda: at + 305)
            await worker.tick()
            await worker.tick()
            self.assertEqual(db.virtual_stats()[0]["wins"], 1)
            self.assertEqual(bot.send_message.await_count, 1)
            self.assertEqual(provider.candles.await_count, 1)


if __name__ == "__main__":
    unittest.main()
