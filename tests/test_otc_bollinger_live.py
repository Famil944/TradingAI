from contextlib import closing
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
from services.otc_delivery_guard import claim
from services.otc_market_archive import MarketArchive
from services.otc_strategy import Candle


class QualityTests(unittest.TestCase):
    def test_reentry_requires_directional_rejection_and_complete_evidence(self):
        from services.otc_bollinger_live import quality_rejection
        base = dict(direction="UP", lower=99.5, upper=102,
                    candle=dict(open=100, high=101, low=99, close=100.5))
        self.assertIsNone(quality_rejection(base))
        for close, expected in [(99.4, "no_reentry"), (100, "weak_rejection")]:
            with self.subTest(close=close):
                self.assertEqual(quality_rejection(dict(base, candle=dict(base["candle"], close=close))), expected)
        self.assertEqual(quality_rejection({"direction": "UP"}), "missing_evidence")
        down = dict(direction="DOWN", lower=98, upper=100.5,
                    candle=dict(open=100, high=101, low=99, close=99.5))
        self.assertIsNone(quality_rejection(down))
        self.assertEqual(quality_rejection(dict(down, candle=dict(down["candle"], close=100))), "weak_rejection")


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

    def test_same_pair_can_have_independent_M2_M5_M15_reservations(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = CycleJournal(Path(folder) / "cycles.db", per_horizon=True)
            entry = 9000
            for seconds in (120, 300, 900):
                self.assertIsNotNone(
                    journal.reserve(self.plan(entry, seconds), 8980),
                    f"M{seconds // 60} should not be blocked by other expiries",
                )
            # Every horizon still rejects a second signal of its own expiry.
            for seconds in (120, 300, 900):
                self.assertIsNone(journal.reserve(
                    dict(self.plan(entry + seconds, seconds), symbol="B"),
                    entry + seconds - 20,
                ))

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
    async def test_live_s2_s4_candidates_generated_even_without_s1(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = BollingerCycles(CycleJournal(Path(folder)/"cycles.db"), clock=lambda:7180)
            snapshot = ({}, {}, {}, None)
            with patch.object(engine.live_candidates, "plan", new_callable=AsyncMock) as generate:
                await engine.plan(SimpleNamespace(send_message=AsyncMock()), 1, snapshot)
                generate.assert_awaited_once_with(snapshot)

    async def test_plan_sends_one_card_and_registers_three_endpoints(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = CycleJournal(Path(folder) / "cycles.db", per_horizon=True)
            engine = BollingerCycles(journal, clock=lambda: 7180)
            engine.allowed_timeframes = (120,)
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
                return_value={"bollinger_reversal": {"direction": "UP", "lower": 99.5, "upper": 102, "candle": {"open": 100, "high": 101, "low": 99, "close": 100.5}}},
            ):
                await engine.plan(bot, 1, snapshot)
                await engine.plan(bot, 1, snapshot)
            bot.send_message.assert_awaited_once()
            self.assertEqual(len(journal.pending()), 1)
            self.assertTrue({7200, 7320, 7440, 7560} <= engine.sampler.targets["A"])
            self.assertIn("БОЛЛИНДЖЕР ×3 · M2", bot.send_message.call_args.args[1])

    async def test_s1_only_sends_m2_even_at_shared_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = CycleJournal(Path(folder) / "cycles.db", per_horizon=True)
            engine = BollingerCycles(journal, clock=lambda: 8981)
            bars = [
                Candle(8940 - i * 60, 100, 101, 99, 100.5)
                for i in range(60, 0, -1)
            ]
            asset = SimpleNamespace(
                open=True, payout=95, observed_at=8981, name="Asset"
            )
            snapshot = ({"A": bars}, {"A": asset}, {"A": 8981}, None)
            engine.observe(*snapshot)
            message_ids = iter((1, 2, 3))
            async def send_message(*_args, **_kwargs):
                return SimpleNamespace(message_id=next(message_ids))
            bot = SimpleNamespace(send_message=AsyncMock(side_effect=send_message))
            with patch("services.otc_bollinger_live.check_data", return_value=None), \
                 patch(
                     "services.otc_bollinger_live.pattern_directions",
                     return_value={"bollinger_reversal": {"direction": "UP", "lower": 99.5, "upper": 102, "candle": {"open": 100, "high": 101, "low": 99, "close": 100.5}}},
                 ):
                await engine.plan(bot, 777, snapshot)
            self.assertEqual(bot.send_message.await_count, 1)
            pending = journal.pending()
            self.assertEqual(
                {r["expires_at"] - r["entry_at"] for r in pending},
                {360},
            )

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

    async def test_first_win_releases_shared_claim_for_other_strategies(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            clock = [2080]
            journal = CycleJournal(root / "cycles.db", per_horizon=True)
            engine = BollingerCycles(journal, clock=lambda: clock[0])
            plan = LiveCycleTests().plan()
            identifier = journal.reserve(plan, 2080)
            journal.sent(identifier, 2080, message_id=1)
            guard = root / "signal-delivery.db"
            self.assertTrue(claim(guard, plan, engine.label, 2080))
            engine.research.samples([
                ("A", 2100, 2100, 2100, 100),
                ("A", 2400, 2400, 2400, 101),
            ])
            clock[0] = 2400
            await engine.settle_pending({})
            next_plan = dict(plan, entry_at=2400, expires_at=3300)
            self.assertTrue(claim(guard, next_plan, "bb_reentry", 2390))

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
            with closing(sqlite3.connect(journal.path)) as db, db:
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
            with closing(sqlite3.connect(journal.path)) as db, db:
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
            with closing(sqlite3.connect(journal.path)) as db, db:
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
