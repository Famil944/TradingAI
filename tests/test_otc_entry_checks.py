import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from database.db import Database
from services.otc_conditional import Setup, make_plan
from services.otc_entry_checks import EntryChecks, decide, message
from services.otc_strategy import Candle


class EntryCheckTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, seconds=60):
        at = 1800000000
        plan = make_plan(
            "A", at, Setup("UP", "red", 0.5, "fixture"), at, seconds=seconds
        )
        row = dict(
            asdict(plan),
            id="fixture",
            direction="UP",
            colour="red",
            invalidation=0.5,
            reason="fixture",
            lifecycle="ACTIVE",
            delivery="SENT",
        )
        bars = [
            Candle(t, 1.1, 1.2, 0.8, 0.9)
            for t in range(plan.condition_from, plan.entry_at, 60)
        ]
        return plan, row, bars

    async def test_never_confirm_before_close_and_missing_full_candle(self):
        for seconds in (60, 300, 900):
            plan, row, bars = self.fixture(seconds)
            self.assertIsNone(decide(row, bars, plan.entry_at - 1)[0])
            self.assertEqual(decide(row, bars, plan.entry_at + 2)[0], "MET")
            self.assertIsNone(decide(row, bars[:-1], plan.entry_at + 2)[0])
            self.assertEqual(decide(row, bars[:-1], plan.entry_at + 30)[0], "NO_DATA")
            self.assertEqual(
                decide({**row, "lifecycle": "CANCELLED"}, bars, plan.entry_at + 2)[0],
                "CANCELLED",
            )

    async def test_wrong_colour_and_late_confirmation_not_entry_instruction(self):
        plan, row, bars = self.fixture()
        bars[0] = Candle(bars[0].time, 0.9, 1.2, 0.8, 1.1)
        self.assertEqual(decide(row, bars, plan.entry_at + 2)[0], "SKIPPED")
        self.assertIn("ПРОПУСК", message(row, "MET", plan.entry_at + 10))
        self.assertIn("Задержка 10 с", message(row, "MET", plan.entry_at + 10))

    async def test_claim_is_persistent_even_when_telegram_send_fails(self):
        plan, row, bars = self.fixture()
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "bot.db")
            db.init_db()
            identifier = db.prepare_plan(plan, plan.setup_at)
            db.plan_delivered(identifier, plan.setup_at, True)
            bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError()))
            provider = SimpleNamespace(
                usage=AsyncMock(return_value=SimpleNamespace(remaining=100)),
                candles=AsyncMock(return_value=bars),
            )
            service = EntryChecks(
                db, provider, bot, 1, lambda: plan.entry_at + 2, require_quality=False
            )
            await service.tick()
            await service.tick()
            self.assertEqual(bot.send_message.await_count, 1)
            self.assertEqual(db.entry_check_targets(plan.entry_at + 3), [])
            with db.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT decision FROM otc_entry_checks"
                    ).fetchone()[0],
                    "MET",
                )
