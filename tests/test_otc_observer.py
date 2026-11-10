import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from database.db import Database
from services.otc_conditional import Setup, condition_met, make_plan, message
from services.otc_market import Candle
from services.otc_ml.features import FEATURE_NAMES
from services.otc_observer import STRATEGY, observer_setup, publish


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "observer.db")
        self.db.init_db()
        self.at = 1800000000
        self.plan = make_plan(
            "#FB_otc", self.at, Setup("UP", "green", 0.5, "fixture"), self.at
        )
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.stop = Path(self.temp.name) / "stop"
        self.patcher = patch("services.otc_observer.STOP_FILE", self.stop)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    async def test_separate_label_and_journal_never_assume_manual_trade(self):
        sent = await publish(self.bot, self.db, self.plan, lambda: self.at)
        self.assertTrue(sent)
        self.assertIn("НАБЛЮДАТЕЛЬ", self.bot.send_message.call_args.args[1])
        self.assertIn("Facebook OTC", self.bot.send_message.call_args.args[1])
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM otc_conditional_plans").fetchone()
            self.assertEqual(row["strategy"], STRATEGY)
            self.assertEqual(row["outcome"], "UNKNOWN")
        self.assertEqual(self.db.stats(), [])

    async def test_stop_marker_prevents_publishing(self):
        self.stop.touch()
        self.assertFalse(await publish(self.bot, self.db, self.plan, lambda: self.at))
        self.bot.send_message.assert_not_called()

    async def test_late_delivery_is_not_success(self):
        now = [self.at]

        async def delayed(*args):
            now[0] += 61

        self.bot.send_message.side_effect = delayed
        self.assertFalse(await publish(self.bot, self.db, self.plan, lambda: now[0]))
        self.assertEqual(self.db.plan_stats()["late"], 1)

    async def test_observer_rule_uses_only_closed_data_and_different_confirmation(self):
        bars = [Candle(i * 60, 1, 1.1, 0.9, 1) for i in range(120)]
        at = 120 * 60
        future = [Candle(at, 1, 1e9, 0.1, 1e8)]
        features = {name: 0.0 for name in FEATURE_NAMES}
        features.update(adx14=0.1, rsi14=0.3, bb_position=0.1, atr14=0.01, range_atr=1)

        def values(prefix):
            self.assertTrue(all(b.time + 60 <= at for b in prefix))
            return [features[n] for n in FEATURE_NAMES]

        with patch("services.otc_observer.indicators", side_effect=values):
            result = observer_setup(bars + future, at)
        self.assertEqual(result.direction, "UP")
        self.assertEqual(result.colour, "green")
        plan = make_plan("A", at, result, at)
        self.assertTrue(condition_met(plan, Candle(at + 120, 1, 1.2, 0.95, 1.1)))
        self.assertFalse(condition_met(plan, Candle(at + 120, 1, 1.1, 0.9, 0.95)))

    async def test_compact_message_keeps_time_condition_and_deadline(self):
        text = message(self.plan, "fixture")
        self.assertEqual(len(text.splitlines()), 4)
        self.assertIn("Facebook OTC", text)
        self.assertIn("Вход ВВЕРХ", text)
        self.assertIn("Если свеча", text)
        self.assertIn("Получить до", text)


if __name__ == "__main__":
    unittest.main()
