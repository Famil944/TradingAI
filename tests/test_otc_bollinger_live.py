import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.otc_bollinger_live import (
    BollingerCycles,
    CycleJournal,
    card,
    live_outcome,
)
from services.otc_market_archive import MarketArchive
from services.otc_strategy import Candle


class LiveCycleTests(unittest.TestCase):
    def plan(self, entry=2100, seconds=300):
        return dict(
            symbol="A",
            name="Asset",
            entry_at=entry,
            expires_at=entry + 3 * seconds,
            cycle_seconds=seconds,
            direction="UP",
            payout=90,
            model_sha256="test",
        )

    def test_m5_reserve_uses_five_minute_boundary_not_fifteen(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = CycleJournal(Path(folder) / "cycles.db", per_horizon=True)
            plan = self.plan()
            identifier = journal.reserve(plan, 2080)
            self.assertIsNotNone(identifier)
            journal.sent(identifier, 2080)
            journal.complete(identifier, ["LOSS", "WIN", "PENDING"], "WIN", 2702)
            self.assertIsNone(journal.reserve(self.plan(2700), 2680))
            self.assertIsNotNone(journal.reserve(self.plan(3000), 2980))
            self.assertIn("② 1", journal.stats(since=0))

    def test_no_fabricated_cycle_win_after_missing_or_draw(self):
        self.assertEqual(live_outcome(["LOSS", "WIN", "PENDING"]), "WIN")
        self.assertEqual(live_outcome(["UNKNOWN", "WIN", "WIN"]), "UNKNOWN")
        self.assertEqual(live_outcome(["DRAW", "WIN", "WIN"]), "DRAW")
        self.assertEqual(live_outcome(["LOSS"] * 3), "LOSS")
        self.assertEqual(live_outcome(["LOSS", "PENDING", "PENDING"]), "PENDING")
        text = card(self.plan(), ["WIN", "PENDING", "PENDING"], "WIN")
        self.assertEqual(text.count("отменена"), 2)
        self.assertIn("M5", text)


class LiveCycleAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_sends_one_card_and_registers_three_endpoints(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = CycleJournal(Path(folder) / "cycles.db", per_horizon=True)
            engine = BollingerCycles(journal, clock=lambda: 7180)
            engine.allowed_timeframes = (300,)
            bars = [
                Candle(7140 - i * 60, 100, 101, 99, 100.5) for i in range(60, 0, -1)
            ]
            asset = SimpleNamespace(
                open=True, payout=90, observed_at=7180, name="Asset"
            )
            snapshot = ({"A": bars}, {"A": asset}, {"A": 7180}, None)
            engine.observe(*snapshot)
            bot = SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=7))
            )
            with patch(
                "services.otc_bollinger_live.pattern_directions",
                return_value={"bollinger_reversal": {"direction": "UP"}},
            ):
                await engine.plan(bot, 1, snapshot)
                await engine.plan(bot, 1, snapshot)
            bot.send_message.assert_awaited_once()
            self.assertEqual(len(journal.pending()), 1)
            self.assertTrue({7200, 7500, 7800, 8100} <= engine.sampler.targets["A"])
            self.assertIn("БОЛЛИНДЖЕР ×3 · M5", bot.send_message.call_args.args[1])

    async def test_first_win_settles_early_and_preserves_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            clock = [2080]
            journal = CycleJournal(root / "cycles.db", per_horizon=True)
            engine = BollingerCycles(journal, clock=lambda: clock[0])
            plan = LiveCycleTests().plan()
            identifier = journal.reserve(plan, 2080)
            journal.sent(identifier, 2080, message_id=1)
            engine.research.samples(
                [("A", 2100, 2100, 2100, 100), ("A", 2400, 2400, 2400, 101)]
            )
            clock[0] = 2403
            await engine.settle_pending({})
            self.assertEqual(journal.pending(), [])
            self.assertIn("① 1", journal.stats(since=0))
            bot = SimpleNamespace(edit_message_text=AsyncMock())
            await engine.update_cards(bot, 1)
            self.assertIn("Цикл: ✅ WIN", bot.edit_message_text.call_args.args[0])

    async def test_confirmed_expiry_tick_settles_without_fixed_two_second_delay(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            clock = [2080]
            journal = CycleJournal(root / "cycles.db", per_horizon=True)
            engine = BollingerCycles(journal, clock=lambda: clock[0])
            identifier = journal.reserve(LiveCycleTests().plan(), 2080)
            journal.sent(identifier, 2080, message_id=1)
            engine.research.samples(
                [("A", 2100, 2100, 2100, 100),
                 ("A", 2400, 2400, 2400, 101)]
            )
            clock[0] = 2400
            await engine.settle_pending({})
            self.assertEqual(journal.pending(), [])
            with sqlite3.connect(journal.path) as db:
                state, evidence = db.execute(
                    "SELECT outcome,evidence FROM predictions WHERE id=?",
                    (identifier,),
                ).fetchone()
            self.assertEqual(state, "WIN")
            self.assertEqual(json.loads(evidence)["attempt_sources"], ["otcharts_tick"])

    async def test_missing_quote_waits_for_exact_canonical_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            clock = [2080]
            journal = CycleJournal(root / "cycles.db", per_horizon=True)
            engine = BollingerCycles(journal, clock=lambda: clock[0])
            identifier = journal.reserve(LiveCycleTests().plan(), 2080)
            journal.sent(identifier, 2080, message_id=1)
            # A later quote is not the price at expiry and cannot settle it.
            engine.research.samples(
                [("A", 2100, 2100, 2100, 100),
                 ("A", 2401, 2401, 2401, 101)]
            )
            clock[0] = 2403
            await engine.settle_pending({})
            self.assertEqual(len(journal.pending()), 1)
            self.assertEqual(engine.card_cache[identifier], ["PENDING"] * 3)

            # Late official history has both exact, unrevised candle opens.
            MarketArchive(engine.archive_path).bars(
                "A", [Candle(2100, 100, 100, 100, 100),
                      Candle(2400, 101, 101, 101, 101)],
                2500,
            )
            clock[0] = 2500
            await engine.settle_pending({})
            self.assertEqual(journal.pending(), [])
            with sqlite3.connect(journal.path) as db:
                state, evidence = db.execute(
                    "SELECT outcome,evidence FROM predictions WHERE id=?",
                    (identifier,),
                ).fetchone()
            self.assertEqual(state, "WIN")
            self.assertEqual(json.loads(evidence)["attempt_sources"], ["otcharts_history"])

    async def test_missing_quote_only_becomes_unknown_after_grace(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            clock = [2080]
            journal = CycleJournal(root / "cycles.db", per_horizon=True)
            engine = BollingerCycles(journal, clock=lambda: clock[0])
            identifier = journal.reserve(LiveCycleTests().plan(), 2080)
            journal.sent(identifier, 2080, message_id=1)
            clock[0] = 2579  # First attempt expires at 2400; 179s of grace.
            await engine.settle_pending({})
            self.assertEqual(len(journal.pending()), 1)
            clock[0] = 2581  # The settlement loop polls every two seconds.
            await engine.settle_pending({})
            self.assertEqual(journal.pending(), [])
            with sqlite3.connect(journal.path) as db:
                state, evidence = db.execute(
                    "SELECT outcome,evidence FROM predictions WHERE id=?",
                    (identifier,),
                ).fetchone()
            self.assertEqual(state, "UNKNOWN")
            self.assertEqual(json.loads(evidence)["attempt_outcomes"],
                             ["UNKNOWN", "PENDING", "PENDING"])

    async def test_no_send_without_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = CycleJournal(Path(folder) / "cycles.db", per_horizon=True)
            engine = BollingerCycles(journal, clock=lambda: 2080)
            bot = SimpleNamespace(send_message=AsyncMock())
            await engine.plan(bot, 1, ({}, {}, {}, None))
            bot.send_message.assert_not_awaited()
