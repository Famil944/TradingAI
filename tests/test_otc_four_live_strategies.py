"""Four independently delivered strategies: causality, isolation, concise UI."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from services.otc_candidate_research import CandidateJournal
from services.otc_community_cycles import VERSION
from services.otc_strategies_live import (
    LiveStrategyJournal, LiveStrategyPublisher, candidate_list,
    evaluate_cycle, load_live_signals,
)
from services.otc_public_stats import metrics, metric_units
from services.otc_public_broadcast import PublicPublisher
from services.otc_subscribers import Subscribers, SubscriberRelay


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
        self.bot = FakeBot()

    async def test_three_independent_strategies_delivered_with_distinct_names(self):
        self.candidates.record([
            candidate("bb_reentry", "BTC"),
            candidate("break_retest", "ETH"),
            candidate("ema_pullback", "AUDCHF"),
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
            candidate("bb_reentry", "BTC"),
            candidate("break_retest", "ETH"),
            candidate("ema_pullback", "AUDCHF"),
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
            relay = SubscriberRelay(root, store=store, clock=lambda: 1005)
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
            relay = SubscriberRelay(root, store=store, clock=lambda: 1005)
            await relay.update(bot)
            self.assertEqual(len(bot.sent), 1)
            self.assertIn("Пробой и ретест", bot.sent[0][1])
            self.assertNotIn("ФОН", bot.sent[0][1])
            await relay.update(bot)
            self.assertEqual(len(bot.sent), 1)

    def test_canonical_recovery_requires_exact_unrevised_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "market-history.db"
            with sqlite3.connect(archive) as db:
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
            with sqlite3.connect(archive) as db:
                db.execute("UPDATE bars SET revisions=1 WHERE time=1140")
            attempts, sources, state = evaluate_cycle(
                row, {}, 1400, archive
            )
            self.assertEqual(state, "UNKNOWN")

    def test_compact_overview_has_four_named_winrates(self):
        from bot import community
        with tempfile.TemporaryDirectory() as folder:
            labels = [
                button.text
                for row in community.sources().inline_keyboard
                for button in row
            ]
            self.assertEqual(sum("①" in x or "②" in x or "③" in x or "④" in x
                                 for x in labels), 4)
            self.assertFalse(any("ФОН" in x for x in labels))
            self.assertLessEqual(len(community.sources().inline_keyboard), 3)
            overview = community.overview_text()
            self.assertIn("ЧЕТЫРЕ СТРАТЕГИИ", overview)
            self.assertEqual(overview.count("WR "), 4)


if __name__ == "__main__":
    unittest.main()
