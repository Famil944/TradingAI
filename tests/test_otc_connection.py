"""Network timeouts must never masquerade as session expiry."""

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from database.db import Database
from services.otc_data import AccessUnavailable, OTChartsProvider, RetryableFeedError
from services.otc_market import Tick
from services.otc_session import OTCSession
from tests.test_otc_core import FakeProvider


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
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
