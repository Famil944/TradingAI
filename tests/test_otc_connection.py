from contextlib import closing
"""Network timeouts must never masquerade as session expiry."""

import asyncio
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from database.db import Database
from services.otc_data import (
    AccessUnavailable,
    OTChartsProvider,
    RetryableFeedError,
    StreamSlotBusy,
)
from services.otc_market import Asset, Tick
from services.otc_market_archive import MarketArchive
from services.otc_readiness import DataReadiness
from services.otc_session import OTCSession, db_call
from services.otc_strategy import Candle
from tests.test_otc_core import FakeProvider


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_429_distinguishes_exhausted_quota_from_busy_stream(self):
        response = SimpleNamespace(status=429, headers={"Retry-After": "43"})
        with self.assertRaisesRegex(AccessUnavailable, "квота запросов"):
            OTChartsProvider._check(response)
        with self.assertRaises(StreamSlotBusy) as error:
            OTChartsProvider._check(response, stream=True)
        self.assertEqual(error.exception.retry_after, 43)
        response.headers["Retry-After"] = "not-a-number"
        with self.assertRaises(StreamSlotBusy) as error:
            OTChartsProvider._check(response, stream=True)
        self.assertEqual(error.exception.retry_after, 60)

    async def test_stream_lifecycle_records_open_time_and_closes_on_cancel(self):
        class Response:
            status = 200
            headers = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

        response = Response()
        stamp = time.time()

        async def content():
            yield (
                f'data: {{"symbol":"A","time":{stamp + 7200},"price":1.2}}\n'
            ).encode()

        response.content = content()
        provider = OTChartsProvider("offline-test")
        provider._http = AsyncMock(
            return_value=SimpleNamespace(get=Mock(return_value=response))
        )
        stream = provider.ticks(["A"])
        await anext(stream)
        self.assertTrue(provider.connected)
        self.assertIsNotNone(provider.stream_opened_at)
        await stream.aclose()
        self.assertFalse(provider.connected)

    async def test_transient_stream_sqlite_lock_recovers_without_stopping(self):
        calls = [0]
        resumed = asyncio.Event()
        hold = asyncio.Event()

        async def ticks(symbols):
            calls[0] += 1
            if calls[0] == 1:
                raise sqlite3.OperationalError("database is locked")
            resumed.set()
            yield Tick("A", self.clock[0], 1)
            await hold.wait()

        self.provider.ticks = ticks
        self.session._restore_stream_history = AsyncMock(return_value={})
        async def paused(*_):
            await hold.wait()
        self.session._refresh_stream_assets = paused
        with patch("services.otc_session.reconnect_delay", return_value=0):
            scan = asyncio.create_task(self.session._scan(self.bot, {"A": []}, {"A": None}))
            try:
                await asyncio.wait_for(resumed.wait(), 3)
                self.assertGreaterEqual(calls[0], 2)
                self.assertGreaterEqual(self.session._health_counts["sqlite_busy"], 1)
                self.assertFalse(scan.done())
            finally:
                scan.cancel()
                await asyncio.gather(scan, return_exceptions=True)

    async def test_sqlite_lock_retries_but_schema_errors_fail_closed(self):
        operation = Mock(side_effect=[sqlite3.OperationalError("database is locked"),
                                      sqlite3.OperationalError("database is busy"), 5])
        with patch("services.otc_session.asyncio.sleep", new_callable=AsyncMock):
            self.assertEqual(await db_call(operation), 5)
        self.assertEqual(operation.call_count, 3)
        fatal = Mock(side_effect=sqlite3.OperationalError("no such table: signals"))
        with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
            await db_call(fatal)
        fatal.assert_called_once()

    async def test_readiness_db_lock_cannot_stop_safe_stream_warmup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "readiness.db"
            readiness = DataReadiness(path, now=1000)
            with closing(sqlite3.connect(path, timeout=1)) as db, db:
                db.execute("BEGIN EXCLUSIVE")
                readiness.reset(1010, "stream_reset")
                self.assertTrue(readiness.gap_open)
                self.assertEqual(readiness.ticks, {})
                with patch("services.otc_readiness.check_data", return_value=None):
                    result = {}
                    for when in (1010, 1015, 1021):
                        result = readiness.observe(
                            Tick("A", when, 1), when, {}, {}, {"A": when}
                        )
                self.assertEqual(result, {"A": 1021})
            readiness.reset(1030, "after_lock")

    async def test_concurrent_history_downloads_serialize_archive_writes(self):
        busy = [0]
        maximum = [0]

        def save(*_):
            busy[0] += 1
            maximum[0] = max(maximum[0], busy[0])
            time.sleep(.01)
            busy[0] -= 1

        archive = SimpleNamespace(bars=save)
        provider = OTChartsProvider("offline-test", archive=archive)
        now = self.clock[0]
        provider._get = AsyncMock(return_value={
            "candles": [dict(time=now + 7200 - 60, open=1, high=1,
                             low=1, close=1)]
        })
        await asyncio.gather(
            provider.candles("A", now),
            provider.candles("B", now),
        )
        self.assertEqual(maximum[0], 1)

    async def test_archive_bulk_read_preserves_duplicate_tick_revisions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bars.db"
            archive = MarketArchive(path)
            archive.bars("A", [Candle(60, 1, 1, 1, 1),
                               Candle(60, 2, 2, 2, 2)], 180, "otcharts_ticks")
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(
                    db.execute("SELECT open,revisions FROM bars").fetchone(),
                    (2, 1),
                )

    @staticmethod
    def continuous_bars(now, count=600):
        last = int(now) // 60 * 60 - 60
        return [Candle(last - 60 * i, 1, 1, 1, 1) for i in range(count - 1, -1, -1)]

    async def test_short_reconnect_reuses_verified_history_without_five_calls(self):
        now = self.clock[0]
        history = {s: self.continuous_bars(now) for s in ("A", "B")}
        self.provider.candles = AsyncMock()
        self.provider.symbols = AsyncMock(return_value=[
            Asset(s, True, True, 92, now, s, now) for s in ("A", "B")
        ])
        chosen = {s: None for s in history}
        active = await self.session._restore_stream_history(chosen, history)
        self.assertEqual(set(active), {"A", "B"})
        self.provider.candles.assert_not_awaited()

    async def test_long_reconnect_only_refetches_stale_history_two_at_once(self):
        now = self.clock[0]
        history = {"A": self.continuous_bars(now)}
        for symbol in ("B", "C", "D"):
            history[symbol] = self.continuous_bars(now - 180)
        self.provider.symbols = AsyncMock(return_value=[
            Asset(s, True, True, 92, now, s, now) for s in history
        ])
        current = [0]
        peak = [0]
        async def candles(symbol, _):
            current[0] += 1
            peak[0] = max(peak[0], current[0])
            await asyncio.sleep(.01)
            current[0] -= 1
            return self.continuous_bars(now)
        self.provider.candles = AsyncMock(side_effect=candles)
        active = await self.session._restore_stream_history(dict.fromkeys(history), history)
        self.assertEqual(set(active), set(history))
        self.assertEqual(self.provider.candles.await_count, 3)
        self.assertLessEqual(peak[0], 2)
        self.assertTrue(all(self.session._can_reuse_stream_history(v, now) for v in history.values()))

    async def test_history_refusal_is_not_hidden_by_parallel_fetch(self):
        self.provider.candles = AsyncMock(side_effect=AccessUnavailable("invalid key"))
        with self.assertRaises(AccessUnavailable):
            await self.session._restore_stream_history({"A": None}, {})
        self.provider.candles.assert_awaited_once()

    async def test_reconnect_waits_for_single_stream_slot_release(self):
        plan = await self.provider.usage()
        self.session._usage_with_retry = AsyncMock(
            return_value=replace(plan, streams_open=1)
        )
        self.session._wait_rotation_slot = AsyncMock()
        await self.session._restore_stream_history({"A": None}, {})
        self.session._wait_rotation_slot.assert_awaited_once()

    async def test_busy_stream_slot_does_not_spend_catalogue_or_candle_calls(self):
        plan = await self.provider.usage()
        self.session._usage_with_retry = AsyncMock(
            return_value=replace(plan, streams_open=1)
        )
        self.session._wait_rotation_slot = AsyncMock(
            side_effect=AccessUnavailable("slot not released")
        )
        self.provider.symbols = AsyncMock()
        self.provider.candles = AsyncMock()
        with self.assertRaises(AccessUnavailable):
            await self.session._restore_stream_history({"A": None}, {})
        self.provider.symbols.assert_not_awaited()
        self.provider.candles.assert_not_awaited()

    async def test_fresh_cached_catalogue_skips_paid_reconnect_request(self):
        now = self.clock[0]
        self.provider.catalogue = {
            "A": Asset("A", True, True, 95, now - 10, "A", now - 10)
        }
        self.provider.symbols = AsyncMock()
        history = {"A": self.continuous_bars(now)}
        active = await self.session._restore_stream_history({"A": None}, history)
        self.assertIn("A", active)
        self.provider.symbols.assert_not_awaited()

    async def test_stale_cached_catalogue_must_be_refreshed(self):
        now = self.clock[0]
        self.provider.catalogue = {
            "A": Asset("A", True, True, 95, now - 50, "A", now - 50)
        }
        self.provider.symbols = AsyncMock(return_value=[
            Asset("A", True, True, 95, now, "A", now)
        ])
        history = {"A": self.continuous_bars(now)}
        await self.session._restore_stream_history({"A": None}, history)
        self.provider.symbols.assert_awaited_once()

    async def test_only_our_closed_aged_stream_can_replace_busy_slot(self):
        plan = await self.provider.usage()
        self.session._usage_with_retry = AsyncMock(
            return_value=replace(plan, streams_open=1)
        )
        self.session._wait_stream_release = AsyncMock()
        with patch("services.otc_session.time.monotonic", return_value=1000):
            self.provider.stream_opened_at = 930
            self.provider.connected = False
            await self.session._wait_rotation_slot()
            self.session._wait_stream_release.assert_not_awaited()
            self.provider.connected = True
            with self.assertRaises(RetryableFeedError):
                await self.session._wait_rotation_slot()
            self.assertEqual(self.session._wait_stream_release.await_count, 3)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.db")
        self.db.init_db()
        self.clock = [1800000000.0]
        self.provider = FakeProvider(self.clock)
        config = SimpleNamespace(
            otcharts_api_key="",
            max_instruments=3,
            cooldown_seconds=900,
            max_tick_age=8,
            max_tick_gap=8,
            payout_max_age=60,
            settlement_tolerance=2,
        )
        self.session = OTCSession(self.db, self.provider, config, lambda: self.clock[0])
        self.bot = SimpleNamespace(send_message=AsyncMock())

    async def asyncTearDown(self):
        await self.session.stop()
        self.temp.cleanup()

    async def test_usage_timeout_is_retried_and_reported_as_network_failure(self):
        self.provider.usage = AsyncMock(
            side_effect=TimeoutError("private transport detail")
        )
        with patch("services.otc_session.reconnect_delay", return_value=0):
            await self.session.start(self.bot, 1, 30, (60,))
            self.assertIn("ещё не определены", self.session.status())
            await asyncio.wait_for(self.session.task, 3)
        self.assertEqual(self.provider.usage.await_count, 3)
        self.assertIn("после повторных попыток", self.session.last_error)
        self.assertNotIn(
            "Время OTC-сеанса закончилось", self.bot.send_message.call_args.args[1]
        )
        self.assertNotIn(
            "private transport detail", self.bot.send_message.call_args.args[1]
        )

    async def test_inner_timeout_is_distinct_from_real_deadline(self):
        self.session._run = AsyncMock(side_effect=TimeoutError())
        await self.session.start(self.bot, 1, 30, (60,))
        await self.session.task
        self.assertIn("сетевой тайм-аут", self.session.last_error)
        self.assertNotIn(
            "Время OTC-сеанса закончилось", self.bot.send_message.call_args.args[1]
        )

    async def test_auth_refusal_is_not_retried(self):
        self.provider.usage = AsyncMock(
            side_effect=AccessUnavailable("HTTP 401: invalid key")
        )
        await self.session.start(self.bot, 1, 30, (60,))
        await self.session.task
        self.assertEqual(self.provider.usage.await_count, 1)
        self.assertIn("401", self.session.last_error)

    async def test_transient_failures_can_recover_inside_session(self):
        plan = await self.provider.usage()
        self.provider.usage = AsyncMock(
            side_effect=[RetryableFeedError(), TimeoutError(), plan]
        )
        with patch("services.otc_session.reconnect_delay", return_value=0):
            await self.session.start(self.bot, 1, 30, (60,))
            for _ in range(100):
                if self.session.phase == "поток активен":
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(self.session.phase, "поток активен")
        self.assertEqual(self.provider.usage.await_count, 3)
        self.assertIsNone(self.session.last_error)

    async def test_adapter_sanitizes_network_timeout(self):
        provider = OTChartsProvider("offline-test")

        def broken(*args, **kwargs):
            raise TimeoutError("credentials must not leak")

        provider._http = AsyncMock(return_value=SimpleNamespace(get=broken))
        with self.assertRaises(RetryableFeedError) as error:
            await provider._get("/v1/usage")
        self.assertNotIn("credentials", str(error.exception))

    async def check_stalled_stream(self, events):
        self.session.config.stream_stale_timeout = 0.03
        connections = []
        closed = []
        ready = asyncio.Event()

        async def stream(symbols):
            connections.append(1)
            try:
                if len(connections) > 1:
                    ready.set()
                    await self.provider.release.wait()
                else:
                    yield Tick("A", self.clock[0], 1)
                    if events == "silent":
                        await self.provider.release.wait()
                    else:
                        while True:
                            await asyncio.sleep(0.002)
                            stamp = (
                                self.clock[0] - 100
                                if events == "stale"
                                else self.clock[0]
                            )
                            yield Tick("A", stamp, 1)
            finally:
                closed.append(1)

        self.provider.ticks = stream
        with patch("services.otc_session.reconnect_delay", return_value=0):
            await self.session.start(self.bot, 1, 30, (60,))
            await asyncio.wait_for(ready.wait(), 2)
            self.assertGreaterEqual(len(connections), 2)
            self.assertTrue(closed)
            self.assertEqual(self.session.monitored, 0)
            self.assertEqual(self.db.stats(), [])
            with self.db.connect() as c:
                self.assertIsNotNone(
                    c.execute(
                        "SELECT 1 FROM otc_journal_events WHERE reason='stream_no_fresh_ticks'"
                    ).fetchone()
                )
            await self.session.stop()
            self.assertFalse(self.session.running())

    async def test_quiet_stream_is_closed_and_reconnected(self):
        await self.check_stalled_stream("silent")

    async def test_continuous_stale_ticks_do_not_keep_stream_alive(self):
        await self.check_stalled_stream("stale")

    async def test_duplicate_ticks_do_not_reset_freshness_deadline(self):
        await self.check_stalled_stream("duplicate")

    async def test_health_report_explains_silence_without_trade_direction(self):
        await self.session._event("invalid_or_stale_tick", "A")
        await self.session._event("no_candidate_M1", "A")
        await self.session._health_report(self.bot)
        text = self.bot.send_message.call_args.args[1]
        self.assertIn("КОНТРОЛЬ РАБОТЫ", text)
        self.assertIn("старые или недопустимые тики", text)
        self.assertIn("условия стратегии не выполнены (M1)", text)
        self.assertNotIn("CALL / UP", text)
        self.assertNotIn("PUT / DOWN", text)
        self.assertEqual(self.db.stats(), [])
        self.assertFalse(self.session._health_counts)

    async def test_compact_health_only_notifies_failure_and_recovery(self):
        self.session.config.compact_messages = True
        self.session.last_tick = self.clock[0]
        await self.session._health_report(self.bot)
        self.bot.send_message.assert_not_called()
        self.session.last_tick = None
        await self.session._health_report(self.bot)
        await self.session._health_report(self.bot)
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.session.last_tick = self.clock[0]
        await self.session._health_report(self.bot)
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_fresh_prices_do_not_hide_stale_payout_metadata(self):
        self.session.config.compact_messages = True
        self.session.config.require_paid_metadata = True
        self.session.last_tick = self.clock[0]
        self.provider.catalogue = {}
        await self.session._health_report(self.bot)
        self.assertIn("выплатах", self.bot.send_message.call_args.args[1])

    async def test_usage_failure_cannot_block_symbol_refresh(self):
        active = {}
        self.provider.usage = AsyncMock(side_effect=TimeoutError())
        with patch(
            "services.otc_session.asyncio.sleep",
            new=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.session._refresh_stream_assets(active, {"A": None})
        self.assertIn("A", active)
        self.provider.usage.assert_not_awaited()

    async def test_history_recovery_survives_temporary_api_failure(self):
        plan = await self.provider.usage()
        self.session._usage_with_retry = AsyncMock(
            side_effect=[RetryableFeedError(), plan]
        )
        history = {}
        with patch("services.otc_session.reconnect_delay", return_value=0):
            active = await self.session._restore_stream_history({"A": None}, history)
        self.assertIn("A", history)
        self.assertIn("A", active)
        self.assertEqual(self.session._usage_with_retry.await_count, 2)

    async def test_history_recovery_does_not_retry_invalid_access(self):
        self.session._usage_with_retry = AsyncMock(
            side_effect=AccessUnavailable("invalid key")
        )
        with self.assertRaises(AccessUnavailable):
            await self.session._restore_stream_history({"A": None}, {})
        self.session._usage_with_retry.assert_awaited_once()

    async def test_recent_signal_suppresses_redundant_health_notification(self):
        self.session.last_signal_at = self.clock[0]
        await self.session._health_report(self.bot)
        self.bot.send_message.assert_not_called()

    async def test_two_minute_cooldown_is_minimum_not_forced_direction(self):
        from services.otc_market import Asset

        self.session.config.cooldown_seconds = 120
        self.session.chat_id = 1
        for offset in (0, 119, 120):
            now = self.clock[0] + offset
            self.session.clock = lambda at=now: at
            await self.session._send(
                self.bot,
                Tick("A", now, 1),
                Asset("A", True, True, 95, now),
                60,
                ("UP", "fixture"),
                now,
            )
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(self.session.session_signal_count, 2)
        self.assertEqual(self.db.stats()[0]["total"], 2)

    async def test_health_watchdog_is_cancelled_with_session(self):
        self.session.config.health_report_seconds = 0.01
        await self.session.start(self.bot, 1, 30, (60,))
        for _ in range(100):
            if self.bot.send_message.await_count:
                break
            await asyncio.sleep(0.005)
        self.assertGreater(self.bot.send_message.await_count, 0)
        await self.session.stop()
        count = self.bot.send_message.await_count
        await asyncio.sleep(0.03)
        self.assertEqual(self.bot.send_message.await_count, count)

    async def test_busy_stream_slot_can_clear_without_stopping_session(self):
        free = await self.provider.usage()
        self.provider.usage = AsyncMock(
            side_effect=[replace(free, streams_open=1), free]
        )
        self.session._wait_stream_release = AsyncMock()
        self.session._scan = AsyncMock()
        await self.session._run(self.bot)
        self.assertEqual(self.provider.usage.await_count, 2)
        self.session._wait_stream_release.assert_awaited_once()
        self.session._scan.assert_awaited_once()

    async def test_failed_service_notification_does_not_crash_session(self):
        self.bot.send_message.side_effect = TimeoutError("private transport detail")
        await self.session._notify(self.bot, "health")
        self.bot.send_message.assert_awaited_once()

    async def test_slow_catalogue_does_not_pause_reading_fresh_ticks(self):
        self.session.config.catalogue_refresh_seconds = 0.01
        blocked = asyncio.Event()
        metadata_closed = asyncio.Event()
        ticks = []
        calls = 0
        original = self.provider.symbols

        async def symbols():
            nonlocal calls
            calls += 1
            if calls == 1:
                return await original()
            blocked.set()
            try:
                await self.provider.release.wait()
            finally:
                metadata_closed.set()
            return await original()

        async def stream(symbols):
            while True:
                self.clock[0] += 0.01
                ticks.append(self.clock[0])
                yield Tick("A", self.clock[0], 1)
                await asyncio.sleep(0.005)

        self.provider.symbols = symbols
        self.provider.ticks = stream
        await self.session.start(self.bot, 1, 30, (60,))
        await asyncio.wait_for(blocked.wait(), 3)
        before = len(ticks)
        for _ in range(100):
            if len(ticks) > before:
                break
            await asyncio.sleep(0.01)
        self.assertGreater(len(ticks), before)
        self.assertEqual(self.session.phase, "поток активен")
        await self.session.stop()
        self.assertTrue(metadata_closed.is_set())

    async def test_real_deadline_expiry_still_reports_elapsed_time(self):
        self.session.chat_id = 1
        self.session.ends_at = self.clock[0] - 1

        # An actual await, not a pre-raised network TimeoutError.
        async def wait(bot):
            await asyncio.sleep(10)

        self.session._run = wait
        await self.session._timed_run(self.bot)
        self.assertIn(
            "Время OTC-сеанса закончилось", self.bot.send_message.call_args.args[1]
        )
        self.assertIsNone(self.session.last_error)


if __name__ == "__main__":
    unittest.main()


class CatalogueStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_catalogue_failure_retries_but_auth_does_not(self):
        provider = SimpleNamespace(
            symbols=AsyncMock(side_effect=[RetryableFeedError(), []])
        )
        fake = SimpleNamespace(provider=provider)
        with patch("services.otc_session.asyncio.sleep", new_callable=AsyncMock):
            self.assertEqual(await OTCSession._catalogue_with_retry(fake), [])
        self.assertEqual(provider.symbols.await_count, 2)
        provider.symbols = AsyncMock(side_effect=AccessUnavailable("HTTP 401"))
        with self.assertRaises(AccessUnavailable):
            await OTCSession._catalogue_with_retry(fake)
        self.assertEqual(provider.symbols.await_count, 1)
