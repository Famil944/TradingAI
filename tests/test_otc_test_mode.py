"""Research-only unknown metadata and free REST snapshot integration."""

import json
import math
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from database.db import Database
from services.otc_data import AccessUnavailable, Plan
from services.otc_market import Asset, Tick, eligible
from services.otc_session import OTCSession
from services.otc_strategy import Candle


def history():
    prices = [100 + i * 0.002 + math.sin(i * 0.3) * 0.04 for i in range(901)]
    return [
        Candle(
            1800000000 + i * 60,
            prices[max(0, i - 1)],
            max(prices[max(0, i - 1)], p) + 0.002,
            min(prices[max(0, i - 1)], p) - 0.002,
            p,
        )
        for i, p in enumerate(prices)
    ]


class ResearchPolicyTests(unittest.TestCase):
    def test_unknown_and_low_payout_allowed_only_for_research(self):
        rows = [
            Asset("A", True, None, None, None),
            Asset("B", True, True, 70, None),
            Asset("C", True, False, 99, 100),
            Asset("D", False, None, None, None),
        ]
        self.assertEqual(
            [a.symbol for a in eligible(rows, 100, research=True)], ["A", "B"]
        )
        self.assertEqual(eligible(rows, 100), [])

    def test_v1_migration_preserves_rows_indexes_and_backup(self):
        with tempfile.TemporaryDirectory() as d:
            current = Database(Path(d) / "current.db")
            current.init_db()
            with current.connect() as c:
                ddl = c.execute(
                    "SELECT sql FROM sqlite_master WHERE name='otc_journal_signals'"
                ).fetchone()[0]
            old = Path(d) / "old.db"
            ddl = ddl.replace(
                "payout REAL, payout_at REAL,",
                "payout REAL NOT NULL, payout_at REAL NOT NULL,",
            )
            self.assertIn("payout REAL NOT NULL", ddl)
            with closing(sqlite3.connect(old)) as c:
                c.execute(ddl)
                c.execute("CREATE TABLE otc_journal_meta(version INTEGER NOT NULL)")
                c.execute("INSERT INTO otc_journal_meta VALUES(1)")
                c.execute(
                    "CREATE INDEX custom_journal_idx ON otc_journal_signals(source)"
                )
                c.commit()
            db = Database(old)
            old_id = db.record(
                symbol="OLD",
                direction="UP",
                timeframe=60,
                signal_at=100,
                price=1,
                payout=90,
                payout_at=100,
                reason="preserved",
                quality={},
                source="otcharts",
            )
            db.init_db()
            db.init_db()
            with db.connect() as c:
                row = c.execute("SELECT * FROM otc_journal_signals").fetchone()
                self.assertEqual(row["id"], old_id)
                self.assertEqual(row["payout"], 90)
                self.assertEqual(row["reason"], "preserved")
                self.assertEqual(
                    c.execute("SELECT version FROM otc_journal_meta").fetchone()[0], 2
                )
                self.assertIsNotNone(
                    c.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='custom_journal_idx'"
                    ).fetchone()
                )
                flags = {
                    r["name"]: r["notnull"]
                    for r in c.execute("PRAGMA table_info(otc_journal_signals)")
                }
                self.assertEqual(flags["payout"], 0)
                self.assertEqual(flags["payout_at"], 0)
            copies = list(Path(d).glob("old.db.backup-*"))
            self.assertEqual(len(copies), 1)
            with closing(sqlite3.connect(copies[0])) as c:
                self.assertEqual(
                    c.execute("SELECT id FROM otc_journal_signals").fetchone()[0],
                    old_id,
                )
            new_id = db.record(
                symbol="NEW",
                direction="UP",
                timeframe=60,
                signal_at=200,
                price=1,
                payout=None,
                payout_at=None,
                reason="unknown",
                quality={},
                source="otcharts",
            )
            self.assertIsNotNone(new_id)


class FreeSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.init_db()
        self.now = 1800000000 + 901 * 60 + 2
        self.plan = Plan("free", 200, 200, "month", self.now + 86400, 0, 0, 0, ("otc",))
        self.provider = SimpleNamespace(
            source="otcharts",
            usage=AsyncMock(return_value=self.plan),
            symbols=AsyncMock(return_value=[Asset("A", True, None, None, None)]),
            candles=AsyncMock(return_value=history()),
            ticks=AsyncMock(),
            close=AsyncMock(),
        )
        self.config = SimpleNamespace(
            otcharts_api_key="",
            test_mode=True,
            max_instruments=3,
            cooldown_seconds=900,
            max_tick_age=8,
            max_tick_gap=8,
            payout_max_age=60,
            settlement_tolerance=2,
            snapshot_max_age=30,
            poll_seconds=300,
            poll_request_budget=2,
        )
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.service = OTCSession(self.db, self.provider, self.config, lambda: self.now)
        self.service.chat_id = 1

    async def asyncTearDown(self):
        await self.service.stop()
        self.temp.cleanup()

    def signal_messages(self):
        return [
            c.args[1]
            for c in self.bot.send_message.call_args_list
            if "CALL / UP" in c.args[1] or "PUT / DOWN" in c.args[1]
        ]

    async def test_free_snapshot_real_strategy_unknown_payout_is_sent(self):
        await self.service._timed_run(self.bot)
        messages = self.signal_messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("выплата: неизвестна", messages[0])
        self.assertIn("ТЕСТОВЫЙ РЕЖИМ", messages[0])
        self.assertIn("Снимок свечей, live-потока нет", messages[0])
        self.assertEqual(self.provider.candles.await_count, 1)
        self.assertEqual(self.provider.symbols.await_count, 1)
        self.assertEqual(self.service.poll_requests_used, 2)
        self.provider.ticks.assert_not_called()
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM otc_journal_signals").fetchone()
            self.assertIsNone(row["payout"])
            self.assertIsNone(row["payout_at"])
            self.assertEqual(row["signal_at"], self.now)
            self.assertEqual(row["expires_at"], self.now + 60)
            self.assertEqual(row["outcome"], "UNKNOWN")
            self.assertTrue(row["strategy"].endswith("-test-snapshot"))
            quality = json.loads(row["quality"])
            self.assertTrue(quality["test_mode"])
            self.assertFalse(quality["payout_confirmed"])
            self.assertFalse(quality["open_confirmed"])
        self.assertIsNone(self.db.stats()[0]["expectancy"])

    async def test_strict_mode_still_refuses_zero_stream_plan(self):
        self.config.test_mode = False
        with self.assertRaises(AccessUnavailable):
            await self.service._run(self.bot)
        self.provider.candles.assert_not_called()
        self.assertEqual(self.db.stats(), [])

    async def test_stale_snapshot_does_not_emit_signal(self):
        self.now += 60
        await self.service._timed_run(self.bot)
        self.assertEqual(self.signal_messages(), [])
        with self.db.connect() as c:
            self.assertEqual(
                c.execute("SELECT reason FROM otc_journal_events").fetchone()[0],
                "stale_snapshot",
            )

    async def test_no_history_and_confirmed_closed_are_not_fabricated(self):
        self.provider.candles.return_value = []
        await self.service._timed_run(self.bot)
        self.assertEqual(self.signal_messages(), [])
        self.provider.symbols.return_value = [Asset("A", True, False, None, None)]
        with self.assertRaises(AccessUnavailable):
            await self.service._run(self.bot)

    async def test_reserve_and_session_budget_enforced(self):
        self.plan = Plan("free", 2, 200, "month", self.now + 86400, 0, 0, 0, ("otc",))
        self.provider.usage.return_value = self.plan
        with self.assertRaises(AccessUnavailable):
            await self.service._run(self.bot)
        self.provider.symbols.assert_not_called()
        self.provider.candles.assert_not_called()

    async def test_repeated_snapshot_is_not_resent_and_respects_budget(self):
        self.config.poll_request_budget = 4
        self.service._wait_poll = AsyncMock()
        await self.service._timed_run(self.bot)
        self.assertEqual(self.provider.candles.await_count, 3)
        self.assertEqual(self.service.poll_requests_used, 4)
        self.assertEqual(len(self.signal_messages()), 1)

    async def test_unknown_metadata_stream_signal_and_stale_payout_label(self):
        self.service.transport = "live"
        tick = Tick("A", self.now, 1)
        await self.service._send(
            self.bot,
            tick,
            Asset("A", True, None, 80, self.now - 1000),
            60,
            ("UP", "test"),
            self.now,
        )
        messages = self.signal_messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("80% (актуальность не подтверждена)", messages[0])
        self.assertTrue(self.db.pending()[0]["strategy"].endswith("-test-live"))

    async def test_known_low_payout_is_reported_without_false_uncertainty(self):
        await self.service._send(
            self.bot,
            Tick("A", self.now, 1),
            Asset("A", True, True, 80, self.now),
            60,
            ("UP", "test"),
            self.now,
        )
        self.assertIn("80% (подтверждена)", self.signal_messages()[0])
        self.assertTrue(json.loads(self.db.pending()[0]["quality"])["payout_confirmed"])

    async def test_strict_mode_unknown_metadata_cannot_enter_journal(self):
        self.config.test_mode = False
        await self.service._send(
            self.bot,
            Tick("A", self.now, 1),
            Asset("A", True, None, None, None),
            60,
            ("UP", "test"),
            self.now,
        )
        self.assertEqual(self.signal_messages(), [])
        self.assertEqual(self.db.stats(), [])

    async def test_snapshot_signal_rechecks_freshness_before_send(self):
        self.service.transport = "snapshot"
        await self.service._send(
            self.bot,
            Tick("A", self.now - 31, 1),
            Asset("A", True, None, None, None),
            60,
            ("UP", "test"),
            self.now - 31,
        )
        self.assertEqual(self.signal_messages(), [])

    async def test_snapshot_status_identifies_non_live_transport(self):
        self.service.transport = "snapshot"
        self.assertIn("без live-потока", self.service.status())
        self.assertIn("тестовый", self.service.status())
        self.assertIn("интервал 300 с", self.service.status())


if __name__ == "__main__":
    unittest.main()
