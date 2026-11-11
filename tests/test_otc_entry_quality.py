import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from database.db import Database
from services.otc_conditional import Setup, make_plan
from services.otc_entry_checks import EntryChecks, entry_quality
from services.otc_evaluation import assess
from services.otc_market import Asset
from services.otc_strategy import Candle


class EntryQualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_direction_change_is_rejected_and_future_price_cannot_restore_it(
        self,
    ):
        row = dict(entry_at=1800000180, expires_at=1800000480, direction="UP")
        with patch(
            "services.otc_entry_checks.context_setup",
            return_value=Setup("DOWN", "green", 1, "fixture"),
        ) as model:
            passed, details = entry_quality(row, [])
            self.assertFalse(passed)
            self.assertEqual(details["reason"], "direction_changed")
            model.assert_called_once_with([], 1800000180, 300)
        with patch("services.otc_entry_checks.context_setup", return_value=None):
            self.assertFalse(entry_quality(row, [])[0])

    async def test_payout_and_late_filters_are_persisted_and_counted_as_skips(self):
        for payout, lag, expected in (
            (70, 2, "REJECTED"),
            (92, 8, "LATE"),
            (92, 2, "MET"),
        ):
            with (
                self.subTest(payout=payout, lag=lag),
                tempfile.TemporaryDirectory() as folder,
            ):
                db = Database(Path(folder) / "bot.db")
                db.init_db()
                at = 1800000000
                plan = make_plan("A", at, Setup("UP", "red", 0.5, "fixture"), at)
                identifier = db.prepare_plan(plan, at)
                db.plan_delivered(identifier, at, True)
                now = plan.entry_at + lag
                provider = SimpleNamespace(
                    usage=AsyncMock(return_value=SimpleNamespace(remaining=100)),
                    candles=AsyncMock(
                        return_value=[Candle(plan.condition_from, 1.1, 1.2, 0.8, 0.9)]
                    ),
                    symbols=AsyncMock(
                        return_value=[
                            Asset("A", True, True, payout, None, observed_at=now)
                        ]
                    ),
                )
                bot = SimpleNamespace(send_message=AsyncMock())
                checker = EntryChecks(db, provider, bot, 1, lambda: now)
                with patch(
                    "services.otc_entry_checks.entry_quality",
                    return_value=(True, {"reason": "fixture"}),
                ):
                    await checker.tick()
                row = db.assessment_targets(plan.expires_at + 65)[0]
                self.assertEqual(row["entry_decision"], expected)
                if expected != "MET":
                    self.assertEqual(
                        assess(row, [], plan.expires_at + 65)["outcome"], "SKIPPED"
                    )
                self.assertEqual(bot.send_message.await_count, 1)

    async def test_missing_entry_data_never_becomes_win(self):
        base = dict(lifecycle="ACTIVE", delivery="SENT", expires_at=1800000000)
        self.assertEqual(
            assess({**base, "entry_decision": "NO_DATA"}, [], 1800000100)["outcome"],
            "UNKNOWN",
        )
        self.assertEqual(
            assess({**base, "entry_decision": "REJECTED"}, [], 1800000100)["outcome"],
            "SKIPPED",
        )
