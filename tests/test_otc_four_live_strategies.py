from contextlib import closing
"""Four independently delivered strategies: causality, isolation, concise UI."""
import sqlite3
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.otc_candidate_research import CandidateJournal
from services.otc_community_cycles import VERSION
from services.otc_delivery_guard import claim
from services.otc_public_broadcast import PublicPublisher
from services.otc_public_stats import metrics
from services.otc_strategies_live import (
    LiveStrategyJournal,
    LiveStrategyPublisher,
    evaluate_cycle,
    load_live_signals,
    reconcile_unknown,
)
from services.otc_subscribers import SubscriberRelay, Subscribers


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []
    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))
    async def edit_message_text(self, text, chat_id, message_id, parse_mode=None):
        self.edited.append((chat_id, message_id, text))


def candidate(method, symbol, observed=1000, entry=1020, seconds=120, payout=92):
    return {
        "symbol": symbol, "direction": "UP", "observed_at": observed,
        "entry_at": entry, "expires_at": entry + 3 * seconds,
        "gates": {"cycle": True},
        "payout": payout, "version": method + "-cycles-v1",
        "experiment": {
            "version": VERSION, "method": method, "cycle_seconds": seconds,
            "attempts": 3,
        },
    }


class StrategyLiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "data" / "community-cycles.db"
        self.candidates = CandidateJournal(self.source)
        self.now = [1001]
        self.publisher = LiveStrategyPublisher(
            self.root, clock=lambda: self.now[0]
        )
        # All four strategies now share the 15-20s delivery window.
        self.now[0] = 1001
        self.bot = FakeBot()

    async def test_s5_delivery_deduplicates_and_settles_full_cycle(self):
        self.candidates.record([candidate("exhaustion", "AUDCAD_otc")])
        await self.publisher.publish(self.bot, 777)
        await self.publisher.publish(self.bot, 777)
        self.assertEqual(len(self.bot.sent), 1)
        self.assertIn("S5", self.bot.sent[0][1])
        rows = load_live_signals(self.publisher.journal.path)
        self.assertEqual(rows[0]["strategy"], "exhaustion")
        self.candidates.samples([("AUDCAD_otc", t, t, t, p)
                                 for t, p in [(1020, 100), (1140, 99), (1260, 101)]])
        self.now[0] = 1261
        await self.publisher.settle()
        row = load_live_signals(self.publisher.journal.path)[0]
        self.assertEqual(row["outcome"], "WIN")
        self.assertEqual(row["attempts"], ["LOSS", "WIN"])
        store = Subscribers(self.root / "data" / "subscribers.db")
        self.assertTrue(store.source_preferences(123)["exhaustion"])
        store.set_source(123, "exhaustion", False)
        self.assertFalse(store.source_preferences(123)["exhaustion"])

    async def test_secondary_strategies_share_s1_delivery_window(self):
        self.candidates.record([candidate("bb_reentry", "BTC")])
        self.now[0] = 1001
        await self.publisher.publish(self.bot, 777)
        self.assertEqual(len(self.bot.sent), 1)
        self.now[0] = 1007
        await self.publisher.publish(self.bot, 777)
        self.assertEqual(len(self.bot.sent), 1)

    async def test_independent_sends_do_not_wait_for_other_telegram_replies(self):
        self.candidates.record([
            candidate("bb_reentry", "BTC", seconds=120),
            candidate("break_retest", "ETH", seconds=120),
            candidate("ema_pullback", "AUDCHF", seconds=120),
        ])
        started = []
        ready = asyncio.Event()
        async def send(*args, **kwargs):
            started.append(args)
            if len(started) == 3:
                ready.set()
            await asyncio.wait_for(ready.wait(), 1)
            return SimpleNamespace(message_id=len(started))
        self.bot.send_message = send
        await self.publisher.publish(self.bot, 777)
        self.assertEqual(len(load_live_signals(self.publisher.journal.path)), 3)

    async def test_runtime_rejects_candidate_when_live_snapshot_is_unavailable(self):
        self.candidates.record([candidate("bb_reentry", "BTC")])
        self.publisher.snapshot_provider = lambda: None
        await self.publisher.publish(self.bot, 777)
        self.assertEqual(self.bot.sent, [])

    async def test_disabled_timeframes_are_not_published(self):
        self.candidates.record([
            candidate("bb_reentry", "BTC", seconds=120),
            candidate("break_retest", "BTC", seconds=300),
            candidate("ema_pullback", "BTC", seconds=900),
            candidate("break_retest", "ETH", seconds=120),
        ])
        await self.publisher.publish(self.bot, 777)
        # S2 M2, S3 M5, S4 M15 on the same pair may coexist;
        # the second S3 M2 is also independent of S2 M2.
        self.assertEqual(len(self.bot.sent), 2)
        rows = load_live_signals(self.root / "data" / "community-live.db")
        self.assertEqual(
            {(r["strategy"], r["symbol"], r["seconds"]) for r in rows},
            {("bb_reentry", "BTC OTC", 120),
             ("break_retest", "ETH OTC", 120)},
        )

    async def test_existing_s1_claim_does_not_block_s2(self):
        self.candidates.record([candidate("bb_reentry", "BTC", seconds=120)])
        guard = self.root / "data" / "signal-delivery.db"
        self.assertTrue(claim(guard, dict(
            symbol="BTC", direction="DOWN", entry_at=1020,
            expires_at=3720,
        ), "Боллинджер ×3", 1001))
        await self.publisher.publish(self.bot, 777)
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(len(load_live_signals(
            self.root / "data" / "community-live.db"
        )), 1)

    async def test_verified_win_releases_symbol_before_original_three_attempt_end(self):
        self.candidates.record([candidate("bb_reentry", "BTC")])
        await self.publisher.publish(self.bot, 777)
        self.candidates.samples([
            ("BTC", 1020, 1020, 1020, 100),
            ("BTC", 1140, 1140, 1140, 101),
        ])
        self.now[0] = 1143
        await self.publisher.settle()
        self.assertEqual(load_live_signals(
            self.root / "data" / "community-live.db"
        )[0]["outcome"], "WIN")
        # S3 is allowed while S2 M2 is active: only S2 M2 is locked.
        self.candidates.record([candidate(
            "break_retest", "BTC", observed=1180, entry=1200
        )])
        self.now[0] = 1181
        await self.publisher.publish(self.bot, 777)
        self.assertEqual(len(self.bot.sent), 2)
        # A second S3 M2 waits until the S3 M2 cycle ends at 1560.
        self.candidates.record([candidate(
            "break_retest", "ETH", observed=1360, entry=1380
        )])
        self.now[0] = 1361
        await self.publisher.publish(self.bot, 777)
        self.assertEqual(len(self.bot.sent), 2)
        # The candidate journal retains the rejected ETH proposal at 1380,
        # so use a fresh symbol to test the scoped lock ending at 1560.
        self.candidates.record([candidate(
            "break_retest", "AUDCAD", observed=1540, entry=1560
        )])
        self.now[0] = 1541
        await self.publisher.publish(self.bot, 777)
        self.assertEqual(len(self.bot.sent), 3)

    async def test_three_independent_strategies_delivered_with_distinct_names(self):
        self.candidates.record([
            candidate("bb_reentry", "BTC", seconds=120),
            candidate("break_retest", "ETH", seconds=120),
            candidate("ema_pullback", "AUDCHF", seconds=120),
        ])
        await self.publisher.publish(self.bot, 777)
        self.assertEqual(len(self.bot.sent), 3)
        rows = load_live_signals(self.root / "data" / "community-live.db")
        self.assertEqual({r["strategy"] for r in rows},
                         {"bb_reentry", "break_retest", "ema_pullback"})
        self.assertEqual(len({r["label"] for r in rows}), 3)
        self.assertTrue(all(r["outcome"] == "PENDING" for r in rows))
        self.assertTrue(all(r["origin"] == "live" for r in rows))
        self.assertFalse(any("ФОН" in text for _, text in self.bot.sent))
        await self.publisher.publish(self.bot, 777)
        self.assertEqual(len(self.bot.sent), 3)

    async def test_each_strategy_has_own_winrate_with_no_research_backfill(self):
        self.candidates.record([
            candidate("bb_reentry", "BTC", seconds=120),
            candidate("break_retest", "ETH", seconds=120),
            candidate("ema_pullback", "AUDCHF", seconds=120),
        ])
        await self.publisher.publish(self.bot, 777)
        self.candidates.samples([
            ("BTC", 1020, 1020, 1020.1, 100.0),
            ("BTC", 1140, 1140, 1140.1, 101.0),
            ("ETH", 1020, 1020, 1020.1, 100.0),
            ("ETH", 1140, 1140, 1140.1, 99.0),
            ("AUDCHF", 1020, 1020, 1020.1, 100.0),
            ("AUDCHF", 1140, 1140, 1140.1, 101.0),
        ])
        self.now[0] = 1143
        await self.publisher.settle()
        rows = load_live_signals(self.root / "data" / "community-live.db")
        states = {r["strategy"]: r["outcome"] for r in rows}
        self.assertEqual(states["bb_reentry"], "WIN")
        # Each strategy now uses M2; the losing first attempt remains pending.
        self.assertEqual(states["ema_pullback"], "WIN")
        self.assertEqual(states["break_retest"], "PENDING")
        self.assertEqual(metrics(
            [r for r in rows if r["strategy"] == "bb_reentry"]
        )["wr"], 100.0)
        self.assertEqual(metrics(
            [r for r in rows if r["strategy"] == "break_retest"]
        )["known"], 0)
        await self.publisher.update_owner_cards(self.bot, 777)
        self.assertTrue(any("✅" in text for _, _, text in self.bot.edited))

    async def test_live_result_can_settle_at_confirmed_expiry_boundary(self):
        self.candidates.record([candidate("bb_reentry", "BTC")])
        await self.publisher.publish(self.bot, 777)
        self.candidates.samples([
            ("BTC", 1020, 1020, 1020, 100),
            ("BTC", 1140, 1140, 1140, 101),
        ])
        self.now[0] = 1140
        await self.publisher.settle()
        rows = load_live_signals(self.root / "data" / "community-live.db")
        self.assertEqual(rows[0]["outcome"], "WIN")
        self.assertEqual(rows[0]["attempt_sources"], ["otcharts_tick"])

    async def test_secondary_signals_are_not_sent_late_or_outside_owner_durations(self):
        self.candidates.record([
            candidate("bb_reentry", "BTC", seconds=120),
            candidate("break_retest", "ETH", seconds=300),
        ])
        await self.publisher.publish(
            self.bot, 777, enabled=True, allowed_timeframes=(120,)
        )
        self.assertEqual(len(self.bot.sent), 1)
        self.now[0] = 1019
        await self.publisher.publish(self.bot, 777)
        self.assertEqual(len(self.bot.sent), 1)

    async def test_do_not_publish_legacy_research_rows(self):
        self.candidates.record([candidate("bb_reentry", "BTC")])
        old = self.root / "data" / "community-live.db"
        self.assertEqual(load_live_signals(old), [])
        channel = PublicPublisher(self.root, -100123456)
        self.assertEqual(channel._snapshot(), [])
        self.assertEqual(channel._pending_batches(), [])

    async def test_unverified_results_remain_unknown_and_do_not_fake_losses(self):
        self.candidates.record([candidate("bb_reentry", "BTC")])
        await self.publisher.publish(self.bot, 777)
        self.now[0] = 1390
        await self.publisher.settle()
        rows = load_live_signals(self.root / "data" / "community-live.db")
        self.assertEqual(rows[0]["outcome"], "UNKNOWN")
        self.assertEqual(metrics(rows)["known"], 0)

    async def test_owner_card_updates_do_not_repeat(self):
        self.candidates.record([candidate("bb_reentry", "BTC")])
        await self.publisher.publish(self.bot, 777)
        await self.publisher.update_owner_cards(self.bot, 777)
        self.assertEqual(self.bot.edited, [])
        self.candidates.samples([
            ("BTC", 1020, 1020, 1020.1, 100),
            ("BTC", 1140, 1140, 1140.1, 101),
        ])
        self.now[0] = 1143
        await self.publisher.settle()
        await self.publisher.update_owner_cards(self.bot, 777)
        await self.publisher.update_owner_cards(self.bot, 777)
        self.assertEqual(len(self.bot.edited), 1)


class HistoricalAndSubscriberTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_delivered_signals_are_public_and_subscribed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            journal = LiveStrategyJournal(root / "data" / "community-live.db")
            plan = candidate("bb_reentry", "BTC")
            ident = journal.reserve(plan, 1001)
            journal.not_delivered(ident)
            self.assertEqual(load_live_signals(journal.path), [])
            bot = FakeBot()
            store = Subscribers(root / "data" / "subscribers.db")
            store.subscribe(41, now=990)
            store.grant(41, now=990)
            relay = SubscriberRelay(root, store=store, clock=lambda: 1004)
            await relay.update(bot)
            self.assertEqual(bot.sent, [])

    async def test_live_strategies_are_sent_to_subscribers_automatically(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            journal = LiveStrategyJournal(root / "data" / "community-live.db")
            plan = candidate("break_retest", "AUDCHF")
            ident = journal.reserve(plan, 1001)
            journal.sent(ident, 1001, 1)
            bot = FakeBot()
            store = Subscribers(root / "data" / "subscribers.db")
            store.subscribe(41, now=990)
            store.grant(41, now=990)
            relay = SubscriberRelay(root, store=store, clock=lambda: 1004)
            await relay.update(bot)
            self.assertEqual(len(bot.sent), 1)
            self.assertIn("S3", bot.sent[0][1])
            self.assertNotIn("ФОН", bot.sent[0][1])
            await relay.update(bot)
            self.assertEqual(len(bot.sent), 1)

    def test_export_uses_delivered_signals_not_unpublished_research_by_default(self):
        from scripts.recover_otc_history import export
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            research = CandidateJournal(root / "data" / "community-cycles.db")
            research.record([candidate("bb_reentry", "BTC")])
            journal = LiveStrategyJournal(root / "data" / "community-live.db")
            ident = journal.reserve(candidate("bb_reentry", "BTC"), 1001)
            journal.sent(ident, 1001, 1)
            journal.save_outcome(
                ident, ["WIN"], ["otcharts_tick"], "WIN", 1143
            )
            result = export(root, root / "exports", now=1500)
            self.assertEqual(result["signals"], 1)
            text = (root / "exports" / "signals.csv").read_text(encoding="utf-8-sig")
            self.assertIn("bb_reentry", text)
            self.assertIn("live", text)
            self.assertNotIn("shadow:", text)
            self.assertNotIn("ФОН", text)

    def test_canonical_recovery_requires_exact_unrevised_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "market-history.db"
            with closing(sqlite3.connect(archive)) as db, db:
                db.execute(
                    "CREATE TABLE bars("
                    "symbol TEXT,time INTEGER,open REAL,source TEXT,revisions INTEGER)"
                )
                db.executemany(
                    "INSERT INTO bars VALUES(?,?,?,?,?)",
                    [("BTC", 1020, 100, "otcharts_history", 0),
                     ("BTC", 1140, 101, "otcharts_history", 0)],
                )
            row = dict(symbol="BTC", direction="UP", entry_at=1020, seconds=120)
            attempts, sources, state = evaluate_cycle(
                row, {}, 1143, archive
            )
            self.assertEqual(state, "WIN")
            self.assertEqual(attempts, ["WIN"])
            self.assertEqual(sources, ["otcharts_history"])
            with closing(sqlite3.connect(archive)) as db, db:
                db.execute("UPDATE bars SET revisions=1 WHERE time=1140")
            attempts, sources, state = evaluate_cycle(
                row, {}, 1400, archive
            )
            self.assertEqual(state, "UNKNOWN")

    def test_unknown_recovery_does_not_fabricate_unentered_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "market-history.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute(
                    "CREATE TABLE bars("
                    "symbol TEXT,time INTEGER,open REAL,source TEXT,revisions INTEGER)"
                )
                db.executemany(
                    "INSERT INTO bars VALUES(?,?,?,?,?)",
                    [("BTC", 1020, 100, "otcharts_history", 0),
                     ("BTC", 1140, 99, "otcharts_history", 0)],
                )
            record = {
                "symbol": "BTC", "direction": "UP", "entry_at": 1020,
                "seconds": 120, "attempts_json": '["UNKNOWN"]',
                "attempt_sources_json": '["unconfirmed"]',
            }
            attempts, sources, state = reconcile_unknown(record, {}, 1700, path)
            self.assertEqual(attempts, ["LOSS"])
            self.assertEqual(sources, ["otcharts_history"])
            # No synthetic attempt 2/3: cycle remains UNKNOWN.
            self.assertEqual(state, "UNKNOWN")
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE bars SET open=101 WHERE time=1140")
            attempts, _, state = reconcile_unknown(record, {}, 1700, path)
            self.assertEqual(attempts, ["WIN"])
            self.assertEqual(state, "WIN")

    def test_compact_overview_has_four_named_winrates(self):
        from bot import community
        with tempfile.TemporaryDirectory():
            labels = [
                button.text
                for row in community.sources().inline_keyboard
                for button in row
            ]
            self.assertEqual(sum(x in ("S1", "S2", "S3", "S4") for x in labels), 4)
            self.assertFalse(any("ФОН" in x for x in labels))
            with patch.object(community, "snapshots", return_value=[]), \
                 patch.object(community, "stats_since", return_value=0), \
                 patch.object(community, "reset_note", return_value=""):
                overview = community.overview_text()
            self.assertIn("📊 Винрейт", overview)
            self.assertEqual(overview.count("✅ 0 / ❌ 0"), 5)



if __name__ == "__main__":
    unittest.main()
