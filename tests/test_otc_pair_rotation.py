from contextlib import closing
"""Rotation must preserve active forecasts and stay within a single stream."""

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from services.otc_market import Asset, Tick
from services.otc_pair_rotation import (
    choose_pair_swap, journal_activity, usable_history,
)
from services.otc_session import OTCSession
from services.otc_strategy import Candle


def asset(symbol, payout=92, at=1800000000):
    return Asset(symbol, True, True, payout, at, symbol, at)


def candles(now, count=600):
    last = int(now) // 60 * 60 - 60
    return [
        Candle(last - 60 * i, 1, 1, 1, 1)
        for i in range(count - 1, -1, -1)
    ]


class PairSelectionTests(unittest.TestCase):
    def test_fair_exploration_only_after_twenty_quiet_minutes(self):
        now = 1800000000
        offered = [asset("A", 95), asset("B", 94), asset("C", 93)]
        selected = {a.symbol: a for a in offered[:2]}
        ticks = {"A": now, "B": now}
        since = {"A": now - 1000, "B": now - 1000}
        self.assertIsNone(choose_pair_swap(
            offered, selected, now, since, ticks, ticks, set(), set(), {},
        ))
        since["B"] = now - 1300
        swap = choose_pair_swap(
            offered, selected, now, since, ticks, ticks, set(), set(), {},
        )
        self.assertEqual((swap[0], swap[1].symbol, swap[2]),
                         ("B", "C", "quiet_pair_exploration"))
        self.assertIsNone(choose_pair_swap(
            offered, selected, now, since, ticks, ticks, {"B"}, set(), {},
        ))
        self.assertIsNone(choose_pair_swap(
            offered, selected, now, since, ticks, ticks, set(), {"B"}, {},
        ))

    def test_stale_or_closed_pair_is_replaced_but_never_pending(self):
        now = 1800000000
        offered = [asset("A", 95), asset("B", 94), asset("C", 93)]
        selected = {"A": offered[0], "B": offered[1]}
        since = {"A": now - 1000, "B": now - 1000}
        ticks = {"A": now - 500, "B": now}
        result = choose_pair_swap(
            offered, selected, now, since, ticks, {"B": now}, set(), set(), {},
        )
        self.assertEqual((result[0], result[2]), ("A", "no_fresh_ticks"))
        self.assertIsNone(choose_pair_swap(
            offered, selected, now, since, ticks, {"B": now}, {"A"},
            {"B"}, {},
        ))
        result = choose_pair_swap(
            offered[1:], selected, now, since, {"B": now}, {"B": now},
            set(), set(), {},
        )
        self.assertEqual((result[0], result[2]), ("A", "catalogue_ineligible"))

    def test_recently_removed_pair_cannot_churn_and_unknown_payout_not_added(self):
        now = 1800000000
        offered = [asset("A", 95), asset("B", 94), asset("C", 93)]
        selected = {"A": offered[0], "B": offered[1]}
        since = {"A": now - 3000, "B": now - 3000}
        fresh = {"A": now, "B": now}
        self.assertIsNone(choose_pair_swap(
            offered, selected, now, since, fresh, fresh, set(), set(),
            {"C": now - 100},
        ))
        self.assertIsNone(choose_pair_swap(
            offered[:2] + [asset("C", None)], selected, now, since,
            fresh, fresh, set(), set(), {},
        ))

    def test_no_future_sparse_or_stale_candles_can_warm_new_stream(self):
        now = 1800000010
        good = candles(now)
        self.assertTrue(usable_history(good, now))
        self.assertFalse(usable_history(good[:100], now))
        self.assertFalse(usable_history(good[:-1], now + 250))
        self.assertFalse(usable_history(good[:-1] + [Candle(good[-1].time + 120,
                                                           1, 1, 1, 1)], now))

    def test_pending_and_recent_sent_are_read_from_both_journals(self):
        now = 1800000000
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / "cycles.db"
            second = Path(folder) / "live.db"
            with closing(sqlite3.connect(first)) as db, db:
                db.execute("CREATE TABLE predictions(symbol TEXT,outcome TEXT,sent_at REAL)")
                db.executemany("INSERT INTO predictions VALUES(?,?,?)", [
                    ("A", "PENDING", now - 100),
                    ("B", "WIN", now - 100),
                ])
            with closing(sqlite3.connect(second)) as db, db:
                db.execute("CREATE TABLE signals(symbol TEXT,outcome TEXT,sent_at REAL)")
                db.executemany("INSERT INTO signals VALUES(?,?,?)", [
                    ("C", "LOSS", now - 200),
                    ("D", "UNKNOWN", now - 4000),
                ])
            protected, recent = journal_activity(
                [(first, "predictions"), (second, "signals")], now
            )
            self.assertEqual(protected, {"A"})
            self.assertEqual(recent, {"A", "B", "C"})

    def test_unreadable_journal_never_authorizes_rotation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE unrelated(value INTEGER)")
            self.assertIsNone(journal_activity([(path, "predictions")], 1000))
            self.assertIsNone(journal_activity([(path.with_name("missing.db"),
                                                 "predictions")], 1000))


class PairRotationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = [1800000010.0]
        self.config = SimpleNamespace(
            max_tick_age=8, max_tick_gap=8, payout_max_age=60,
            pair_rotation_seconds=600, pair_rotation_enabled=True,
        )
        self.provider = SimpleNamespace(
            usage=AsyncMock(return_value=SimpleNamespace(
                remaining=500, streams_open=0, streams_limit=1,
            )),
            candles=AsyncMock(return_value=candles(self.now[0])),
        )
        self.session = OTCSession(
            db=None, provider=self.provider, config=self.config,
            clock=lambda: self.now[0],
        )
        self.offered = [
            asset("A", 95, self.now[0]),
            asset("B", 94, self.now[0]),
            asset("C", 93, self.now[0]),
        ]
        self.chosen = {a.symbol: a for a in self.offered[:2]}
        self.session._rotation_selected_at = {
            "A": self.now[0] - 1300, "B": self.now[0] - 1300,
        }
        self.session._rotation_next_at = self.now[0] - 1
        self.session._quotes = {"A": self.now[0], "B": self.now[0]}
        self.session._rotation_last_candle = dict(self.session._quotes)

    async def test_quota_reserve_blocks_new_history_request(self):
        self.provider.usage.return_value = SimpleNamespace(remaining=20)
        await self.session._prepare_rotation(self.offered, self.chosen)
        self.assertIsNone(self.session._rotation_pending)
        self.provider.candles.assert_not_awaited()

    async def test_prepares_only_one_canonical_history_candidate(self):
        await self.session._prepare_rotation(self.offered, self.chosen)
        self.assertIsNotNone(self.session._rotation_pending)
        victim, new, _, reason, _ = self.session._rotation_pending
        self.assertEqual((victim, new.symbol, reason),
                         ("B", "C", "quiet_pair_exploration"))
        await self.session._prepare_rotation(self.offered, self.chosen)
        self.provider.candles.assert_awaited_once()

    async def test_waits_until_single_stream_slot_is_released(self):
        self.session._wait_stream_release = AsyncMock()
        self.session._usage_with_retry = AsyncMock(side_effect=[
            SimpleNamespace(remaining=400, streams_open=1, streams_limit=1),
            SimpleNamespace(remaining=400, streams_open=0, streams_limit=1),
        ])
        await self.session._wait_rotation_slot()
        self.assertEqual(self.session._wait_stream_release.await_count, 2)
        self.assertEqual(self.session._usage_with_retry.await_count, 2)

    async def test_open_cycle_blocks_switch_until_it_is_resolved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cycles.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE predictions(symbol TEXT,outcome TEXT,sent_at REAL)")
                db.execute("INSERT INTO predictions VALUES(?,?,?)",
                           ("B", "PENDING", self.now[0] - 20))
            self.session.cycle_preview = SimpleNamespace(
                journal=SimpleNamespace(path=path)
            )
            history = {"A": candles(self.now[0]), "B": candles(self.now[0])}
            self.session._rotation_pending = (
                "B", self.offered[2], candles(self.now[0]),
                "quiet_pair_exploration", self.now[0],
            )
            self.assertIsNone(await self.session._commit_rotation(self.chosen, history))
            self.assertEqual(set(history), {"A", "B"})
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE predictions SET outcome='WIN'")
            replacement = await self.session._commit_rotation(self.chosen, history)
            self.assertEqual(set(replacement), {"A", "C"})

    async def test_stream_is_closed_before_replacement_and_history_kept(self):
        self.session._rotation_pending = (
            "B", self.offered[2], candles(self.now[0]),
            "quiet_pair_exploration", self.now[0],
        )
        current = [0]
        highest = [0]
        opened = []
        next_stream = asyncio.Event()
        blocking = asyncio.Event()

        async def ticks(symbols):
            current[0] += 1
            highest[0] = max(highest[0], current[0])
            opened.append(tuple(symbols))
            try:
                yield Tick(symbols[0], self.now[0], 1)
                if len(opened) == 2:
                    next_stream.set()
                await blocking.wait()
            finally:
                current[0] -= 1

        self.provider.ticks = ticks
        async def pause_refresh(*_):
            await blocking.wait()

        self.session._refresh_stream_assets = pause_refresh
        self.session._wait_stream_release = AsyncMock()
        history = {"A": candles(self.now[0]), "B": candles(self.now[0])}
        task = asyncio.create_task(self.session._scan(None, history, self.chosen))
        try:
            await asyncio.wait_for(next_stream.wait(), 3)
            self.assertEqual(opened, [("A", "B"), ("A", "C")])
            self.assertEqual(highest[0], 1)
            self.assertEqual(self.session.rotations, 1)
            self.assertIn("C", history)
            self.assertNotIn("B", history)
            self.assertEqual(self.session.active_symbols, ("A", "C"))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
