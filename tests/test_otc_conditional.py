"""Advance delivery, manual condition rules, chronology and plan audit."""

import asyncio
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from database.db import Database
from services.otc_conditional import (
    Setup,
    condition_met,
    make_plan,
    message,
    replay,
    setup,
)
from services.otc_market import Asset, Tick
from services.otc_session import OTCSession
from services.otc_strategy import Candle
from tests.test_otc_test_mode import history


def impulse_history():
    prices = [100 + i * 0.002 + math.sin(i * 0.3 + 0.3) * 0.04 for i in range(901)]
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


class ConditionalRuleTests(unittest.TestCase):
    def test_trend_setup_is_causal_and_requires_no_future_colour(self):
        bars = history()
        at = bars[-1].time + 60
        actual = setup(bars, at)
        self.assertIsNotNone(actual)
        self.assertEqual(actual.direction, "UP")
        self.assertEqual(actual.colour, "red")
        future = [Candle(at + i * 60, 1e8, 1e8, 1e8, 1e8) for i in range(10)]
        self.assertEqual(setup(bars + future, at), actual)
        self.assertIsNone(setup(bars[:20], at))

    def test_scheduling_exact_boundaries_lead_and_session_end(self):
        at = 1800000000
        context = Setup("DOWN", "green", 2, "fixture")
        plan = make_plan("A", at, context, at + 8)
        self.assertEqual(plan.entry_at, at + 180)
        self.assertEqual(plan.condition_from, at + 120)
        self.assertEqual(plan.expires_at, at + 240)
        self.assertEqual(plan.delivery_deadline, at + 60)
        self.assertIsNone(make_plan("A", at, context, at + 31))
        self.assertIsNone(make_plan("A", at, context, at, at + 200))
        self.assertIsNone(make_plan("A", at + 1, context, at + 1))

    def test_manual_green_put_and_red_call_rules_invalidation_and_doji(self):
        at = 1800000000
        down = make_plan("A", at, Setup("DOWN", "green", 2, "fixture"), at)
        self.assertTrue(condition_met(down, Candle(at + 120, 1, 1.6, 0.9, 1.5)))
        for close in (1, 0.8, 2, 2.1):
            self.assertFalse(condition_met(down, Candle(at + 120, 1, 3, 0.5, close)))
        self.assertFalse(condition_met(down, Candle(at + 60, 1, 2, 0.5, 1.5)))
        up = make_plan("A", at, Setup("UP", "red", 0.5, "fixture"), at)
        self.assertTrue(condition_met(up, Candle(at + 120, 1, 1.1, 0.6, 0.8)))
        self.assertFalse(condition_met(up, Candle(at + 120, 1, 1.1, 0.4, 0.5)))

    def test_message_explicit_lateness_manual_check_and_no_second_confirmation(self):
        plan = make_plan(
            "A", 1800000000, Setup("DOWN", "green", 2, "fixture"), 1800000000
        )
        text = message(plan, "example", None, compact=False)
        self.assertIn("ЗЕЛЁНОЙ", text)
        self.assertIn("ВНИЗ / PUT", text)
        self.assertIn("второго подтверждения не будет", text)
        self.assertIn("Получено позже", text)
        self.assertIn("неизвестна", text)
        self.assertIn("отменяются", text)

    def test_conditional_menu_offers_all_expiries(self):
        from bot.otc_commands import menu

        buttons = [b for row in menu(1, conditional=True).inline_keyboard for b in row]
        timeframes = [
            b.callback_data for b in buttons if b.callback_data.startswith("otc:tf:")
        ]
        self.assertEqual(
            timeframes, ["otc:tf:all", "otc:tf:1", "otc:tf:5", "otc:tf:15"]
        )

    def test_replay_uses_future_colour_only_for_evaluation_not_scheduling(self):
        bars = history()
        raw = [
            dict(
                symbol="A",
                time=b.time,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
            )
            for b in bars
        ]
        result = replay(raw, rule="legacy")
        self.assertFalse(result["advantage_confirmed"])
        self.assertTrue(result["plans"])
        self.assertTrue(
            all(p["entry_at"] - p["setup_at"] == 180 for p in result["plans"])
        )
        self.assertTrue(
            all(p["outcome"] in ("UNKNOWN", "SKIPPED") for p in result["plans"])
        )


class ConditionalSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.init_db()
        self.bars = impulse_history()
        self.at = self.bars[-1].time + 60
        self.now = self.at + 5
        self.config = SimpleNamespace(
            otcharts_api_key="",
            signal_mode="conditional",
            test_mode=True,
            cooldown_seconds=120,
            max_tick_age=8,
            snapshot_max_age=30,
            payout_max_age=60,
        )
        self.provider = SimpleNamespace(close=AsyncMock())
        self.session = OTCSession(self.db, self.provider, self.config, lambda: self.now)
        self.session.chat_id = 1
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.asset = Asset("A", True, None, None, None)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def consider(self):
        await self.session._consider_plan(
            self.bot, self.bars, Tick("A", self.at, 1), self.asset, self.at
        )

    async def test_plan_sent_and_audited_separately_without_assuming_execution(self):
        await self.consider()
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(self.session.session_plan_count, 1)
        self.assertEqual(self.db.stats(), [])
        self.assertEqual(self.db.plan_stats()["sent"], 1)
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM otc_conditional_plans").fetchone()
            self.assertEqual(row["entry_at"], self.at + 180)
            self.assertEqual(row["outcome"], "UNKNOWN")
            self.assertIsNone(row["payout"])

    async def test_repeated_or_conflicting_plan_is_not_sent(self):
        await self.consider()
        await self.consider()
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(self.db.plan_stats()["total"], 1)

    async def test_stale_quotes_or_session_ending_refuse_plan(self):
        self.now = self.at + 9
        await self.consider()
        self.assertEqual(self.bot.send_message.await_count, 0)
        self.now = self.at + 5
        self.session.ends_at = self.at + 200
        await self.consider()
        self.assertEqual(self.bot.send_message.await_count, 0)

    async def test_failed_delivery_has_no_retry_or_fake_trade(self):
        self.bot.send_message.side_effect = TimeoutError()
        await self.consider()
        await self.consider()
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(self.db.plan_stats()["failed"], 1)
        self.assertEqual(self.session.session_plan_count, 0)
        self.assertEqual(self.db.stats(), [])

    async def test_late_ack_is_marked_late_and_not_counted_as_delivered_plan(self):
        async def delayed(*args):
            self.now = self.at + 61

        self.bot.send_message.side_effect = delayed
        await self.consider()
        self.assertEqual(self.db.plan_stats()["late"], 1)
        self.assertEqual(self.session.session_plan_count, 0)

    async def test_expiry_and_restart_cancel_do_not_become_wins(self):
        await self.consider()
        self.db.close_plans(self.at + 241)
        self.assertEqual(self.db.plan_stats()["active"], 0)
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM otc_conditional_plans").fetchone()
            self.assertEqual(row["lifecycle"], "EXPIRED")
            self.assertEqual(row["outcome"], "UNKNOWN")

    async def test_live_stream_produces_advance_plan_not_immediate_trade(self):
        from services.otc_data import Plan as AccessPlan
        from tests.test_otc_core import FakeProvider

        clock = [self.bars[900].time]
        self.session.clock = lambda: clock[0]
        self.config.max_tick_gap = 8
        self.config.settlement_tolerance = 2
        self.config.max_instruments = 5
        provider = FakeProvider(clock)
        provider.candles = AsyncMock(return_value=self.bars[:900])
        provider.usage = AsyncMock(
            return_value=AccessPlan(
                "fake", 1000, 1000, "day", clock[0] + 9999, 0, 1, 5, ("otc",)
            )
        )
        self.session.provider = provider
        sent = asyncio.Event()

        async def on_send(*args):
            sent.set()

        self.bot.send_message.side_effect = on_send

        async def stream(symbols):
            bar = self.bars[900]
            prices = [bar.open, bar.low, bar.high] + [bar.close] * 12
            for offset, price in zip(range(0, 60, 4), prices):
                clock[0] = bar.time + offset
                yield Tick("A", clock[0], price)
            clock[0] = bar.time + 60
            yield Tick("A", clock[0], bar.close)
            await provider.release.wait()

        provider.ticks = stream
        try:
            await self.session.start(self.bot, 1, 30, (300, 900))
            await asyncio.wait_for(sent.wait(), 10)
            self.assertEqual(self.session.timeframes, (300, 900))
            self.assertIn("Если свеча", self.bot.send_message.call_args.args[1])
            for _ in range(100):
                if self.db.plan_stats()["sent"] == 2:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(self.db.plan_stats()["total"], 2)
            with self.db.connect() as connection:
                expirations = [
                    r[0]
                    for r in connection.execute(
                        "SELECT expires_at-entry_at FROM otc_conditional_plans ORDER BY expires_at"
                    )
                ]
            self.assertEqual(expirations, [300, 900])
            self.assertEqual(self.db.stats(), [])
        finally:
            await self.session.stop()
        self.assertEqual(self.db.plan_stats()["active"], 0)

    async def test_startup_gap_is_backfilled_without_using_unclosed_future_bar(self):
        from services.otc_data import Plan as AccessPlan

        broken = self.bars[:850] + self.bars[851:]
        self.assertIsNone(setup(broken, self.at))
        self.provider.usage = AsyncMock(
            return_value=AccessPlan(
                "fake", 100, 100, "day", self.at + 999, 0, 1, 5, ("otc",)
            )
        )
        future = Candle(self.at, 1, 1, 1, 1)
        self.provider.candles = AsyncMock(return_value=self.bars + [future])
        cache = {"A": broken}
        await self.session._repair_history("A", cache)
        self.assertEqual(cache["A"], self.bars)
        self.assertIsNotNone(setup(cache["A"], self.at))
        self.assertTrue(all(bar.time + 60 <= self.now for bar in cache["A"]))

    async def test_history_repair_is_rate_limited_per_instrument(self):
        from services.otc_data import Plan as AccessPlan

        self.provider.usage = AsyncMock(
            return_value=AccessPlan(
                "fake", 100, 100, "day", self.at + 999, 0, 1, 5, ("otc",)
            )
        )
        self.provider.candles = AsyncMock(return_value=self.bars)
        tasks = {}
        cache = {"A": []}
        self.session._queue_history_repair("A", cache, tasks)
        self.session._queue_history_repair("A", cache, tasks)
        await asyncio.gather(*tasks.values())
        self.session._queue_history_repair("A", cache, tasks)
        self.assertEqual(self.provider.candles.await_count, 1)
        self.assertEqual(self.session._repair_count, 1)

    async def test_snapshot_mode_still_waits_between_requests(self):
        from services.otc_data import Plan

        self.session.transport = "snapshot"
        self.config.poll_request_budget = 3
        self.provider.usage = AsyncMock(
            return_value=Plan(
                "free", 200, 200, "month", self.at + 99999, 0, 0, 0, ("otc",)
            )
        )
        self.provider.symbols = AsyncMock(return_value=[self.asset])
        self.provider.candles = AsyncMock(return_value=self.bars)
        self.session._wait_poll = AsyncMock()
        await self.session._poll(self.bot, await self.provider.usage())
        self.session._wait_poll.assert_awaited_once()
        self.assertEqual(self.provider.candles.await_count, 2)
        self.assertEqual(self.db.plan_stats()["total"], 3)


if __name__ == "__main__":
    unittest.main()
