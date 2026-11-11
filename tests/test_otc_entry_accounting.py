import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from database.db import Database
from services.otc_conditional import Setup, make_plan
from services.otc_entry_checks import EntryChecks
from services.otc_evaluation import assess
from services.otc_market import Asset
from services.otc_strategy import Candle


class EntryAccountingTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        at = 1800000000
        plan = make_plan("A", at, Setup("UP", "red", 0.5, "fixture"), at)
        row = {
            **asdict(plan),
            "delivery": "SENT",
            "lifecycle": "ACTIVE",
            "direction": "UP",
            "colour": "red",
            "invalidation": 0.5,
            "reason": "fixture",
        }
        bars = [
            Candle(plan.condition_from, 1.1, 1.2, 0.8, 0.9),
            Candle(plan.entry_at, 1, 1.1, 0.9, 1),
            Candle(plan.expires_at, 1.2, 1.3, 1.1, 1.2),
        ]
        return plan, row, bars

    async def test_late_or_unavailable_advisory_does_not_erase_real_loss_or_win(self):
        plan, row, bars = self.fixture()
        for decision in ("LATE", "REJECTED", "NO_DATA"):
            actual = {**row, "entry_decision": decision}
            self.assertEqual(
                assess(actual, bars, plan.expires_at + 65)["outcome"], "WIN"
            )
            losing = bars[:-1] + [Candle(plan.expires_at, 0.8, 0.9, 0.7, 0.8)]
            self.assertEqual(
                assess(actual, losing, plan.expires_at + 65)["outcome"], "LOSS"
            )
            green = [Candle(plan.condition_from, 0.9, 1.2, 0.8, 1.1)] + bars[1:]
            self.assertEqual(
                assess(actual, green, plan.expires_at + 65)["outcome"], "SKIPPED"
            )

    async def test_live_stream_cache_avoids_all_network_reads_for_condition_check(self):
        plan, row, bars = self.fixture()
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "bot.db")
            db.init_db()
            identifier = db.prepare_plan(plan, plan.setup_at)
            db.plan_delivered(identifier, plan.setup_at, True)
            now = plan.entry_at + 3
            asset = Asset("A", True, True, 92, None, observed_at=now - 10)
            provider = SimpleNamespace(
                usage=AsyncMock(), candles=AsyncMock(), symbols=AsyncMock()
            )
            bot = SimpleNamespace(send_message=AsyncMock())
            checker = EntryChecks(
                db,
                provider,
                bot,
                1,
                lambda: now,
                source_snapshot=lambda symbol: (bars[:1], asset),
            )
            with patch(
                "services.otc_entry_checks.entry_quality",
                return_value=(False, {"reason": "context_no_longer_valid"}),
            ):
                await checker.tick()
            provider.usage.assert_not_called()
            provider.candles.assert_not_called()
            provider.symbols.assert_not_called()
            with db.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT decision FROM otc_entry_checks"
                    ).fetchone()[0],
                    "MET",
                )
