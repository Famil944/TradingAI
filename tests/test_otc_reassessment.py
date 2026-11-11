import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from database.db import Database
from services.otc_conditional import Setup, make_plan
from services.otc_reassessment import correct_advisory_outcomes
from services.otc_strategy import Candle


class ReassessmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_correction_has_backup_audit_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "bot.db")
            db.init_db()
            at = 1800000000
            plan = make_plan("A", at, Setup("UP", "red", 0.5, "fixture"), at)
            identifier = db.prepare_plan(plan, at)
            db.plan_delivered(identifier, at, True)
            db.assess_plan(
                identifier,
                dict(
                    outcome="SKIPPED", condition="UNKNOWN", reason="entry_quality_late"
                ),
                plan.expires_at + 65,
            )
            bars = [
                Candle(plan.condition_from, 1.1, 1.2, 0.8, 0.9),
                Candle(plan.entry_at, 1, 1.1, 0.9, 1),
                Candle(plan.expires_at, 1.2, 1.3, 1.1, 1.2),
            ]
            provider = SimpleNamespace(
                usage=AsyncMock(return_value=SimpleNamespace(remaining=100)),
                candles=AsyncMock(return_value=bars),
            )
            result = await correct_advisory_outcomes(db, provider, plan.expires_at + 70)
            self.assertTrue(Path(result["backup"]).exists())
            self.assertEqual(result["corrected"][0]["after"], "WIN")
            with db.connect() as connection:
                audit = connection.execute(
                    "SELECT before_json,after_json FROM otc_assessment_corrections"
                ).fetchone()
                self.assertEqual(json.loads(audit[0])["outcome"], "SKIPPED")
                self.assertEqual(json.loads(audit[1])["outcome"], "WIN")
            self.assertEqual(
                (await correct_advisory_outcomes(db, provider, plan.expires_at + 80))[
                    "corrected"
                ],
                [],
            )

    async def test_missing_prices_do_not_manufacture_corrected_win(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "bot.db")
            db.init_db()
            at = 1800000000
            plan = make_plan("A", at, Setup("UP", "red", 0.5, "fixture"), at)
            identifier = db.prepare_plan(plan, at)
            db.plan_delivered(identifier, at, True)
            db.assess_plan(
                identifier,
                dict(
                    outcome="SKIPPED", condition="UNKNOWN", reason="entry_quality_late"
                ),
                plan.expires_at + 65,
            )
            provider = SimpleNamespace(
                usage=AsyncMock(return_value=SimpleNamespace(remaining=100)),
                candles=AsyncMock(return_value=[]),
            )
            result = await correct_advisory_outcomes(db, provider, plan.expires_at + 70)
            self.assertEqual(result["corrected"], [])
            self.assertEqual(result["waiting"], 1)
