import asyncio
import math
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from database.db import Database
from services.otc_compare import import_comparisons
from services.otc_data import AccessUnavailable, OTChartsProvider, Plan, venue_timestamp
from services.otc_market import (
    Asset,
    MinuteBuilder,
    Tick,
    eligible,
    settle,
    utc_timestamp,
)
from services.otc_replay import replay
from services.otc_session import OTCSession
from services.otc_statistics import metrics, wilson
from services.otc_strategy import (
    Candle,
    aggregate,
    atr,
    candidate,
    ema,
    normalize_candles,
    rsi,
)


class CandleTests(unittest.TestCase):
    def test_seconds_milliseconds_and_documented_venue_offset(self):
        now = 1800000000
        self.assertEqual(utc_timestamp(now * 1000), now)
        self.assertEqual(venue_timestamp(now + 7200), now)
        self.assertEqual(venue_timestamp((now + 7200) * 1000), now)
        for v in (float("nan"), float("inf"), -1):
            with self.assertRaises(ValueError):
                utc_timestamp(v)

    def test_normalize_no_guessed_offset_and_invalid_ohlc(self):
        row = dict(time=1800000000, open=1, high=2, low=0.5, close=1.5)
        self.assertEqual(len(normalize_candles([row], 1800000060)), 1)
        self.assertEqual(normalize_candles([row], 1800000001), [])
        for extra in (
            dict(time=1800007200),
            dict(high=float("nan")),
            dict(low=3),
            dict(close=0),
        ):
            self.assertEqual(normalize_candles([{**row, **extra}], 1800000060), [])
        ms = {**row, "time": row["time"] * 1000}
        self.assertEqual(normalize_candles([row, ms], 1800000060)[0].time, row["time"])
        self.assertEqual(
            normalize_candles([row, {**row, "close": 1.6}], 1800000060), []
        )

    def test_groups_and_boundaries(self):
        bars = [Candle(i * 60, 2, 3, 1, 2.5) for i in range(30)]
        for tf, n in ((60, 30), (300, 6), (900, 2)):
            self.assertEqual(len(aggregate(bars, tf)), n)
        self.assertEqual(aggregate(bars[1:5], 300), [])
        self.assertEqual(aggregate(bars[:4] + bars[5:15], 900), [])
        with self.assertRaises(ValueError):
            aggregate(bars, 120)

    def test_indicators(self):
        self.assertEqual(ema([1, 2, 3, 4, 5], 3), 4)
        self.assertIsNone(ema([1], 3))
        self.assertEqual(rsi([1] * 20), 50)
        self.assertEqual(rsi(list(range(1, 21))), 100)
        self.assertEqual(rsi(list(range(21, 1, -1))), 0)
        bars = [Candle(i * 60, 1, 2, 0.5, 1) for i in range(20)]
        self.assertAlmostEqual(atr(bars), 1.5)

    def test_candidate_no_history_gaps_flat_future_or_unsupported_tf(self):
        bars = [Candle(i * 60, 1, 1.01, 0.99, 1) for i in range(1200)]
        for tf in (60, 300, 900, 120):
            self.assertIsNone(candidate(bars, tf, 72000))
        self.assertIsNone(candidate(bars[:30], 60, 1800))
        self.assertIsNone(candidate(bars[:1150] + bars[1152:], 60, 72000))
        self.assertIsNone(candidate(bars, 60, 71999))

    def test_closed_candidate_ignores_future(self):
        bars = [Candle(i * 60, 1, 1.01, 0.99, 1) for i in range(1200)]
        self.assertEqual(candidate(bars[:1000], 60, 60000), candidate(bars, 60, 60000))

    def test_positive_strategy_and_future_poison_do_not_change_decision(self):
        prices = [100 + i * 0.002 + math.sin(i * 0.3) * 0.04 for i in range(1200)]
        bars = [
            Candle(
                i * 60,
                prices[max(0, i - 1)],
                max(prices[max(0, i - 1)], price) + 0.002,
                min(prices[max(0, i - 1)], price) - 0.002,
                price,
            )
            for i, price in enumerate(prices)
        ]
        result = candidate(bars[:901], 60, 901 * 60)
        self.assertEqual(result[0], "UP")
        poison = bars[:901] + [
            Candle(i * 60, 1e9, 1e9, 1e9, 1e9) for i in range(901, 1200)
        ]
        self.assertEqual(candidate(poison, 60, 901 * 60), result)


class TickTests(unittest.TestCase):
    def test_complete_minute_ohlc_and_boundary(self):
        b = MinuteBuilder()
        prices = []
        for t in range(0, 65, 4):
            p = 1 + t / 100
            prices.append(p)
            result = b.push(Tick("A", t, p), t)
            if t == 60:
                self.assertEqual(result, Candle(0, 1, 1.56, 1, 1.56))
            else:
                self.assertIsNone(result)

    def test_reject_stale_future_invalid_duplicate_unordered(self):
        b = MinuteBuilder()
        for tick in (
            Tick("A", 10, 1),
            Tick("A", 30, 1),
            Tick("A", 20, float("nan")),
            Tick("A", 20, -1),
        ):
            self.assertIsNone(b.push(tick, 20))
            self.assertEqual(b.rejection, "invalid_or_stale_tick")
        b.push(Tick("A", 20, 1), 20)
        for stamp in (20, 19):
            b.push(Tick("A", stamp, 1), 20)
            self.assertEqual(b.rejection, "duplicate_or_unordered_tick")

    def test_internal_gap_or_missing_first_tick_blocks_bar(self):
        for times in (
            [0, 4, 8, 40, 44, 48, 52, 56, 60],
            [4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48, 52, 56, 60],
            [0, 4, 8, 120],
        ):
            b = MinuteBuilder()
            for t in times:
                result = b.push(Tick("A", t, 1), t)
            self.assertIsNone(result)
            self.assertEqual(b.rejection, "incomplete_candle")

    def test_payout_requires_confirmed_open_fresh_evidence(self):
        good = Asset("A", True, True, 90, 100)
        bad = [
            Asset("B", True, None, 95, 100),
            Asset("C", True, True, 99, None),
            Asset("D", True, True, 95, 1),
            Asset("E", False, True, 95, 100),
            Asset("F", True, True, 89, 100),
            Asset("G", True, True, float("nan"), 100),
            Asset("H", True, False, 95, 100),
            Asset("I", True, True, 95, 150),
        ]
        self.assertEqual(eligible([good] + bad, 120), [good])


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.db"
        self.db = Database(self.path)
        self.db.init_db()

    def tearDown(self):
        self.temp.cleanup()

    def record(self, **changes):
        values = dict(
            symbol="A",
            direction="UP",
            timeframe=60,
            signal_at=100,
            price=1,
            payout=90,
            payout_at=99,
            reason="test",
            quality={},
            source="otcharts",
        )
        values.update(changes)
        return self.db.record(**values)

    def test_additive_migration_backup_preserves_existing_otc(self):
        p = Path(self.temp.name) / "old.db"
        with closing(sqlite3.connect(p)) as c:
            c.execute("CREATE TABLE otc_signals(id INTEGER PRIMARY KEY, note TEXT)")
            c.execute("INSERT INTO otc_signals VALUES(1,'preserve')")
            c.commit()
        db = Database(p)
        db.init_db()
        db.init_db()
        copies = list(p.parent.glob("old.db.backup-*"))
        self.assertEqual(len(copies), 1)
        for source in (p, copies[0]):
            with closing(sqlite3.connect(source)) as c:
                self.assertEqual(
                    c.execute("SELECT note FROM otc_signals").fetchone()[0], "preserve"
                )

    def test_cooldown_survives_restart_and_stats_unknown(self):
        identifier = self.record()
        self.assertIsNotNone(identifier)
        self.assertIsNone(self.record(signal_at=110))
        self.assertIsNone(self.record())
        self.db.recover()
        row = self.db.stats()[0]
        self.assertEqual(row["unknown"], 1)
        self.assertIsNone(row["expectancy"])
        self.assertIsNone(
            Database(self.path).record(
                symbol="A",
                direction="DOWN",
                timeframe=60,
                signal_at=120,
                price=1,
                payout=90,
                payout_at=119,
                reason="test",
                quality={},
                source="otcharts",
            )
        )

    def test_settlement_rules_and_same_source(self):
        identifier = self.record(synchronized=True)
        row = self.db.pending()[0]
        self.assertIsNone(settle(row, Tick("A", 159, 2)))
        self.assertEqual(settle(row, Tick("A", 160, 2)), "UNKNOWN")
        self.db.capture_entry(identifier, 100, 1)
        row = self.db.pending()[0]
        for price, outcome in ((2, "WIN"), (0.5, "LOSS"), (1, "DRAW")):
            self.assertEqual(settle(row, Tick("A", 160, price)), outcome)
        self.assertEqual(settle(row, Tick("A", 163, 2)), "UNKNOWN")
        self.assertEqual(
            settle({**row, "source": "other"}, Tick("A", 160, 2)), "UNKNOWN"
        )
        self.db.finish(identifier, "WIN", 2)
        self.assertEqual(self.db.stats()[0]["expectancy"], 0.9)

    def test_independent_price_and_ohlc_comparison(self):
        count = import_comparisons(
            self.db,
            [
                dict(
                    symbol="A",
                    provider_at=100,
                    platform_at=101,
                    provider_price=1,
                    platform_price=1.01,
                    evidence="independent recording",
                    provider_ohlc=dict(open=1, high=2, low=0.5, close=1.5),
                )
            ],
        )
        self.assertEqual(count, 1)
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM otc_journal_comparisons").fetchone()
            self.assertEqual(row["delay"], 1)
            self.assertAlmostEqual(row["deviation"], 0.01 / 1.01)


class ReplayTests(unittest.TestCase):
    def rows(self, n=1100):
        return [
            dict(
                symbol="A",
                time=i * 60,
                open=1,
                high=1.01,
                low=0.99,
                close=1,
                payout=90,
                payout_at=i * 60,
                open_confirmed=True,
            )
            for i in range(n)
        ]

    def test_forward_folds_and_no_future_candle_access(self):
        seen = []

        def strategy(bars, tf, at):
            self.assertTrue(all(b.time + 60 <= at for b in bars))
            seen.append(at)
            return ("UP", "test")

        with patch("services.otc_replay.candidate", strategy):
            report = replay(self.rows(), warmup=900, fold_size=100)
        self.assertTrue(seen)
        self.assertEqual(len(report["folds"]), 2)
        self.assertGreater(report["metrics"]["total"], 0)
        self.assertEqual(report["metrics"]["unknown_share"], 1)
        self.assertIsNone(report["metrics"]["win_rate"])

    def test_invalid_data_and_missing_payout_no_signals(self):
        rows = self.rows()
        rows[10]["time"] = rows[9]["time"]
        with self.assertRaises(ValueError):
            replay(rows)
        rows = self.rows()
        for row in rows:
            row["payout_at"] = None
        with patch("services.otc_replay.candidate", return_value=("UP", "test")):
            self.assertEqual(replay(rows)["metrics"]["total"], 0)

    def test_replay_requires_independent_quotes_for_resolved_results(self):
        rows = self.rows()
        for row in rows:
            row["platform_evidence"] = "fixture-recording"
            row["platform_at"] = row["time"]
            row["platform_open"] = row["open"]
        with patch("services.otc_replay.candidate", return_value=("UP", "test")):
            result = replay(rows, warmup=900)
        self.assertGreater(result["metrics"]["total"], 0)
        self.assertGreater(sum(r["outcome"] == "DRAW" for r in result["signals"]), 0)
        for row in rows:
            row["platform_open"] = 12345
        with patch("services.otc_replay.candidate", return_value=("UP", "test")):
            result = replay(rows, warmup=900)
        self.assertEqual(result["metrics"]["unknown_share"], 1)

    def test_metrics_interval_expectancy_and_drawdown(self):
        result = metrics(
            [
                dict(outcome=o, payout=90)
                for o in ("WIN", "LOSS", "LOSS", "UNKNOWN", "DRAW")
            ]
        )
        self.assertAlmostEqual(result["net_units"], -1.1)
        self.assertEqual(result["max_drawdown_units"], 2)
        self.assertAlmostEqual(result["expectancy"], -1.1 / 4)
        self.assertEqual(result["unknown_share"], 0.2)
        self.assertIsNone(wilson(0, 0))
        lo, hi = wilson(50, 50)
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 0.5)


class FakeProvider:
    source = "otcharts"
    synchronized = False

    def __init__(self, clock):
        self.clock = clock
        self.closed = False
        self.release = asyncio.Event()
        self.catalogue_calls = 0

    async def usage(self):
        return Plan("fake", 1000, 1000, "day", 100000, 0, 1, 2, ("otc",))

    async def symbols(self):
        self.catalogue_calls += 1
        return [Asset("A", True, True, 95, self.clock[0])]

    async def candles(self, symbol, now):
        return []

    async def ticks(self, symbols):
        yield Tick("A", self.clock[0], 1)
        await self.release.wait()

    async def close(self):
        self.closed = True


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.db")
        self.db.init_db()
        self.clock = [1800000000.0]
        self.config = SimpleNamespace(
            otcharts_api_key="",
            max_instruments=3,
            cooldown_seconds=900,
            max_tick_age=8,
            max_tick_gap=8,
            payout_max_age=60,
            settlement_tolerance=2,
        )
        self.provider = FakeProvider(self.clock)
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.session = OTCSession(
            self.db, self.provider, self.config, lambda: self.clock[0]
        )

    async def asyncTearDown(self):
        await self.session.stop()
        self.temp.cleanup()

    async def spin(self):
        for _ in range(50):
            if self.session.phase == "поток активен":
                return
            await asyncio.sleep(0.01)

    async def test_start_stop_race_stale_status_restart(self):
        starts = await asyncio.gather(
            *[self.session.start(self.bot, 1, 0, (60,)) for _ in range(3)]
        )
        self.assertEqual(starts.count(True), 1)
        await self.spin()
        self.assertEqual(self.session.monitored, 1)
        self.clock[0] += 10
        self.assertIn("нет свежих котировок", self.session.status())
        self.assertTrue(await self.session.stop())
        self.assertTrue(self.provider.closed)
        self.assertFalse(self.session.running())
        self.assertEqual(self.session.monitored, 0)
        self.assertTrue(await self.session.start(self.bot, 1, 30, (300,)))
        self.assertNotEqual(self.session.phase, "поток активен")

    async def test_immediate_stop_cleans_up_before_task_first_runs(self):
        await self.session.start(self.bot, 1, 0, (60,))
        await self.session.stop()
        self.assertEqual(self.session.phase, "остановлен")
        self.assertTrue(self.provider.closed)
        self.assertEqual(self.session.monitored, 0)

    async def test_expiry_watchdog_marks_unknown_without_market_ticks(self):
        self.db.record(
            symbol="A",
            direction="UP",
            timeframe=60,
            signal_at=self.clock[0] - 100,
            price=1,
            payout=95,
            payout_at=self.clock[0] - 100,
            reason="test",
            quality={},
            source="otcharts",
        )
        task = asyncio.create_task(self.session._expiry_watchdog())
        try:
            async with asyncio.timeout(5):
                while self.db.stats()[0]["unknown"] != 1:
                    await asyncio.sleep(0.02)
            self.assertEqual(self.db.stats()[0]["unknown"], 1)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_stream_eligibility_is_independent_from_payout(self):
        now = self.clock[0]
        no_payout = Asset("A", True, True, None, None, "A", now)
        self.config.require_paid_metadata = True
        self.assertEqual(
            self.session._eligible(
                [no_payout], now, 60, require_payout=False
            ),
            [no_payout],
        )
        self.assertEqual(self.session._eligible([no_payout], now, 60), [])

    async def test_missing_access_fails_closed(self):
        self.provider.symbols = AsyncMock(
            return_value=[Asset("A", True, None, 95, None)]
        )
        await self.session.start(self.bot, 1, 0, (60,))
        await self.session.task
        self.assertFalse(self.session.running())
        self.assertIn("Нет свежих открытых OTC-инструментов", self.session.last_error)
        self.assertEqual(self.db.stats(), [])

    async def test_delivery_failure_is_unknown_and_not_retried(self):
        self.session.chat_id = 1
        self.bot.send_message.side_effect = RuntimeError("fake")
        tick = Tick("A", self.clock[0], 1)
        asset = Asset("A", True, True, 95, self.clock[0])
        await self.session._send(
            self.bot, tick, asset, 60, ("UP", "test"), self.clock[0]
        )
        await self.session._send(
            self.bot, tick, asset, 60, ("UP", "test"), self.clock[0]
        )
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(self.db.stats()[0]["unknown"], 1)

    async def test_reconnect_clears_continuity(self):
        connections = []

        async def stream(symbols):
            connections.append(1)
            if len(connections) == 1:
                yield Tick("A", self.clock[0], 1)
                raise ConnectionError()
            yield Tick("A", self.clock[0] + 1, 1)
            await self.provider.release.wait()

        self.provider.ticks = stream
        with patch("services.otc_session.reconnect_delay", return_value=0.001):
            await self.session.start(self.bot, 1, 0, (60,))
            for _ in range(100):
                if len(connections) > 1:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(connections), 2)
            self.assertGreaterEqual(self.provider.catalogue_calls, 2)
            self.assertEqual(self.db.stats(), [])

    async def test_real_strategy_tick_to_journal_and_unknown_expiry(self):
        sent = asyncio.Event()

        async def on_send(*args, **kwargs):
            sent.set()

        self.bot.send_message.side_effect = on_send
        base = 1800000000
        prices = [100 + i * 0.002 + math.sin(i * 0.3) * 0.04 for i in range(1200)]
        bars = [
            Candle(
                base + i * 60,
                prices[max(0, i - 1)],
                max(prices[max(0, i - 1)], p) + 0.002,
                min(prices[max(0, i - 1)], p) - 0.002,
                p,
            )
            for i, p in enumerate(prices)
        ]
        self.clock[0] = base + 900 * 60
        self.provider.candles = AsyncMock(return_value=bars[:900])

        async def stream(symbols):
            # Recreate candle 900 with continuous ticks and its exact high/low.
            bar = bars[900]
            values = [bar.open, bar.low, bar.high] + [bar.close] * 12
            for t, price in zip(range(0, 60, 4), values):
                self.clock[0] = bar.time + t
                yield Tick("A", self.clock[0], price)
            self.clock[0] = base + 901 * 60
            yield Tick("A", self.clock[0], bars[901].open)
            # Allow journal observation before session shutdown changes pending to UNKNOWN.
            await self.provider.release.wait()

        self.provider.ticks = stream
        await self.session.start(self.bot, 1, 0, (60,))
        await asyncio.wait_for(sent.wait(), 10)
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertIn("CALL / UP", self.bot.send_message.call_args.args[1])
        self.assertEqual(self.db.stats()[0]["total"], 1)
        await self.session.stop()
        self.assertEqual(self.db.stats()[0]["unknown"], 1)

    async def test_timer_expires_even_without_ticks(self):
        await self.session.start(self.bot, 1, 30, (60,))
        await self.session.stop()
        self.session.ends_at = self.clock[0] - 0.01
        await self.session._timed_run(self.bot)
        self.assertFalse(self.session.running())
        self.assertIn("закончилось", self.bot.send_message.call_args.args[1])

    async def test_no_key_does_not_call_network(self):
        provider = OTChartsProvider("")
        with self.assertRaises(AccessUnavailable):
            await provider.usage()
        self.assertIsNone(provider._session)


if __name__ == "__main__":
    unittest.main()
