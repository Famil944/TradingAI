"""Live deadlines, concurrent fanout and durable duplicate suppression."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.otc_subscribers import SubscriberRelay, Subscribers
from services.otc_bollinger_live import BollingerCycles, CycleJournal


class RelayReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Subscribers(self.root / "users.db")
        self.store.request(42, now=990)
        self.store.grant(42, now=990)
        self.now = 1001
        self.relay = SubscriberRelay(self.root, store=self.store, clock=lambda: self.now)
        self.row = dict(id="live:one", origin="live", strategy="bb_reentry",
                        symbol="TEST OTC", direction="UP", seconds=120,
                        sent_at=1000, entry_at=1020, created_at=1000,
                        outcome="PENDING", attempts=[])
        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)),
            edit_message_text=AsyncMock(),
        )

    async def test_push_and_fallback_race_send_only_once(self):
        async def slow_send(*args, **kwargs):
            await asyncio.sleep(0.02)
            return SimpleNamespace(message_id=7)
        self.bot.send_message.side_effect = slow_send
        with patch.object(self.relay, "_signals", return_value=[self.row]):
            await asyncio.gather(self.relay.publish_now(self.bot, self.row),
                                 self.relay.update(self.bot))
        self.bot.send_message.assert_awaited_once()

    async def test_ambiguous_send_is_not_retried_after_restart(self):
        self.bot.send_message.side_effect = TimeoutError
        await self.relay.publish_now(self.bot, self.row)
        restarted = SubscriberRelay(self.root, store=Subscribers(self.store.path),
                                    clock=lambda: self.now)
        await restarted.publish_now(self.bot, self.row)
        self.bot.send_message.assert_awaited_once()

    async def test_no_new_send_after_deadline_or_before_window(self):
        for now in (999, 1005, 1006, 1010, 1021):
            self.now = now
            await self.relay.publish_now(self.bot, self.row)
        self.bot.send_message.assert_not_awaited()

    async def test_late_ack_is_marked_and_not_counted_as_delivered(self):
        async def delayed_ack(*args, **kwargs):
            self.now = 1006
            return SimpleNamespace(message_id=7)
        self.bot.send_message.side_effect = delayed_ack
        await self.relay.publish_now(self.bot, self.row)
        self.assertIsNone(self.store.delivered(42, self.row["id"]))
        self.assertIn("НЕ ВХОДИТЬ", self.bot.edit_message_text.await_args.args[0])

    async def test_disabling_source_still_updates_existing_card(self):
        await self.relay.publish_now(self.bot, self.row)
        self.store.set_source(42, "bb_reentry", False)
        result = dict(self.row, outcome="WIN", attempts=["WIN"])
        with patch.object(self.relay, "_signals", return_value=[result]):
            await self.relay.update(self.bot)
        self.bot.send_message.assert_awaited_once()
        self.bot.edit_message_text.assert_awaited_once()


class OwnerAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_relay_failure_does_not_cancel_delivered_s1(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = BollingerCycles(CycleJournal(Path(folder) / "cycles.db"),
                                     clock=lambda: 7180,
                                     subscriber_relay=SimpleNamespace(
                                         publish_now=AsyncMock(side_effect=RuntimeError)))
            engine.allowed_timeframes = (120,)
            asset = SimpleNamespace(open=True, payout=93, observed_at=7180, name="TEST")
            snapshot = ({"A": []}, {"A": asset}, {"A": 7180}, None)
            engine.observe(*snapshot)
            bot = SimpleNamespace(send_message=AsyncMock(
                return_value=SimpleNamespace(message_id=7)))
            with patch.object(engine.shadow, "plan", new_callable=AsyncMock), \
                 patch("services.otc_bollinger_live.check_data", return_value=None), \
                 patch("services.otc_bollinger_live.pattern_directions",
                       return_value={"bollinger_reversal": {"direction": "UP"}}):
                await engine.plan(bot, 1, snapshot)
            pending = engine.journal.pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["sent_at"], 7180)
