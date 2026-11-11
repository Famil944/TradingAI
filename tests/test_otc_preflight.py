import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from database.db import Database
from services.otc_conditional import Setup, make_plan
from services.otc_market import Asset
from services.otc_preflight import PreflightChecks, analyze


class PreflightTests(unittest.IsolatedAsyncioTestCase):
    async def check_delivery(self, delay, raises=False):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "bot.db")
            db.init_db()
            at = 1800000000
            plan = make_plan("A", at, Setup("UP", "red", 0.5, "fixture"), at)
            identifier = db.prepare_plan(plan, at)
            db.plan_delivered(identifier, at, True)
            now = [plan.entry_at - 15]
            asset = Asset("A", True, True, 92, None, observed_at=now[0])

            async def send(*args):
                now[0] += delay
                if raises:
                    raise TimeoutError()

            bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
            checker = PreflightChecks(
                db, bot, 1, lambda symbol: ([], asset), lambda: now[0]
            )
            with patch(
                "services.otc_preflight.context_setup", return_value=None
            ) as model:
                await checker.tick()
            with db.connect() as connection:
                lifecycle = connection.execute(
                    "SELECT lifecycle FROM otc_conditional_plans WHERE id=?",
                    (identifier,),
                ).fetchone()[0]
                delivery = connection.execute(
                    "SELECT delivery FROM otc_preflight_checks"
                ).fetchone()[0]
            self.assertEqual(bot.send_message.await_count, 1)
            self.assertLess(model.call_args.args[1], plan.entry_at)
            await checker.tick()
            self.assertEqual(bot.send_message.await_count, 1)
            return lifecycle, delivery

    async def test_only_timely_acknowledged_pre_entry_cancellation_changes_plan(self):
        self.assertEqual(await self.check_delivery(1), ("CANCELLED", "SENT"))
        self.assertEqual(await self.check_delivery(6), ("ACTIVE", "LATE"))
        self.assertEqual(await self.check_delivery(1, True), ("ACTIVE", "FAILED"))

    async def test_after_deadline_does_not_send_or_cancel(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "bot.db")
            db.init_db()
            at = 1800000000
            plan = make_plan("A", at, Setup("UP", "red", 0.5, "fixture"), at)
            identifier = db.prepare_plan(plan, at)
            db.plan_delivered(identifier, at, True)
            bot = SimpleNamespace(send_message=AsyncMock())
            checker = PreflightChecks(
                db, bot, 1, lambda symbol: None, lambda: plan.entry_at - 9
            )
            await checker.tick()
            bot.send_message.assert_not_called()
            with db.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT lifecycle FROM otc_conditional_plans"
                    ).fetchone()[0],
                    "ACTIVE",
                )

    async def test_keep_does_not_claim_future_colour(self):
        row = dict(entry_at=1800000180, expires_at=1800000240, direction="UP")
        now = row["entry_at"] - 15
        asset = Asset("A", True, True, 92, None, observed_at=now)
        with patch(
            "services.otc_preflight.context_setup",
            return_value=Setup("UP", "red", 0.5, "fixture"),
        ):
            decision, reason, evidence = analyze(row, ([], asset), now)
        self.assertEqual(decision, "KEEP")
        self.assertFalse(evidence["future_condition_checked"])
        self.assertLessEqual(evidence["known_asof"], now)
