import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import community
from services.otc_cycle_dashboard import cycle_block, cycle_summary
from services.otc_subscribers import Subscribers, SubscriberRelay
from services.otc_bollinger_live import BollingerCycles, CycleJournal
from services.otc_public_stats import main_signals


def signal(ident, outcome="WIN", attempts=None, seconds=120):
    return dict(id=ident, outcome=outcome, attempts=attempts if attempts is not None else ["WIN"],
                seconds=seconds, sent_at=1000, created_at=995, entry_at=1020,
                origin="main", strategy="bollinger_live", symbol="TEST OTC", direction="UP")


class DashboardTests(unittest.TestCase):
    def test_cycle_wr_and_stages_are_separate_from_attempt_wr(self):
        rows = [signal("one"), signal("two", attempts=["LOSS", "WIN"]),
                signal("three", attempts=["LOSS", "LOSS", "WIN"]),
                signal("loss", "LOSS", ["LOSS"] * 3),
                signal("pending", "PENDING", ["LOSS"]),
                signal("unknown", "UNKNOWN", ["UNKNOWN"]),
                signal("draw", "DRAW", ["DRAW"])]
        m = cycle_summary(rows)
        self.assertEqual(m["wr"], 75)
        self.assertEqual(m["known"], 4)
        self.assertEqual(m["stages"], [1, 1, 1])
        self.assertIn("75.0% · завершено 4", cycle_block("S1", rows))

    def test_missing_attempt_evidence_is_never_invented(self):
        m = cycle_summary([signal("old", attempts=[])])
        self.assertEqual(m["stages"], [0, 0, 0])
        self.assertEqual(m["unclassified_wins"], 1)
        self.assertEqual(m["wr"], 100)

    def test_empty_sample_has_no_percentage(self):
        text = cycle_block("S2", [])
        self.assertIn("WR цикла ×3: —", text)
        self.assertNotIn("0.0%", text)

    def test_filters_survive_all_navigation_and_fit_telegram(self):
        menu = community.cycle_menu("ema_pullback", "30d", "15", "personal")
        actions = [b.callback_data for row in menu.inline_keyboard for b in row]
        self.assertIn("c:cycles:main:30d:15:personal", actions)
        self.assertIn("c:cycles:ema_pullback:7d:15:personal", actions)
        self.assertIn("c:cycles:ema_pullback:30d:2:personal", actions)
        self.assertIn("c:cycles:ema_pullback:30d:15:global", actions)
        self.assertTrue(all(len(x.encode()) <= 64 for x in actions))
        self.assertFalse(any("reset" in x or "c:stats" in x for x in actions))

    def test_personal_view_contains_only_that_users_deliveries(self):
        store = SimpleNamespace(delivered_ids=lambda user: {"mine"} if user == 42 else set())
        with patch.object(community, "subscribers", return_value=store), \
             patch.object(community, "stats_since", return_value=0), \
             patch.object(community, "reset_note", return_value=""), \
             patch.object(community, "snapshots", return_value=[signal("mine"), signal("other", "LOSS", ["LOSS"] * 3)]):
            personal = community.cycle_dashboard("main", scope="personal", user_id=42)
            global_text = community.cycle_dashboard("main")
            self.assertIn("100.0% · завершено 1", personal)
            self.assertIn("50.0% · завершено 2", global_text)
            self.assertNotIn("OTCharts", personal)
            self.assertNotIn("Боллинджер", personal)

    def test_all_four_strategies_stay_independent(self):
        groups = {"main": [signal("s1")], "bb_reentry": [signal("s2", "LOSS", ["LOSS"] * 3)],
                  "break_retest": [], "ema_pullback": [signal("s4", "PENDING", [])]}
        with patch.object(community, "stats_since", return_value=0), \
             patch.object(community, "reset_note", return_value=""), \
             patch.object(community, "snapshots", side_effect=lambda k: groups[k]):
            body = community.cycle_dashboard()
        self.assertIn("S1 · 100.0% · ✅ 1 / ❌ 0", body)
        self.assertIn("S2 · 0.0% · ✅ 0 / ❌ 1", body)
        self.assertIn("S3 · —", body)
        self.assertLess(len(body), 650)

    def test_expiry_filter_does_not_mix_cycle_lengths(self):
        rows = [signal("m2"), signal("m5", "LOSS", ["LOSS"] * 3, seconds=300)]
        with patch.object(community, "stats_since", return_value=0), \
             patch.object(community, "reset_note", return_value=""), \
             patch.object(community, "snapshots", return_value=rows):
            m2 = community.cycle_dashboard("main", timeframe="2")
            m5 = community.cycle_dashboard("main", timeframe="5")
        self.assertIn("100.0% · завершено 1", m2)
        self.assertIn("0.0% · завершено 1", m5)


class AccessTests(unittest.TestCase):
    def test_approval_from_another_old_message_cannot_extend_active_access(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Subscribers(Path(folder) / "users.db")
            store.request(42, now=1000)
            expiry = store.grant(42, now=1000, request_key="notice-1", approval_only=True)
            with self.assertRaisesRegex(ValueError, "уже одобрен"):
                store.grant(42, now=1001, request_key="notice-2", approval_only=True)
            self.assertEqual(store.status(42, now=1001)["expires_at"], expiry)

    def test_duplicate_grant_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "users.db"
            store = Subscribers(path)
            store.request(42, now=1000)
            expiry = store.grant(42, now=1000, request_key="button-1")
            with self.assertRaisesRegex(ValueError, "уже выполнена"):
                Subscribers(path).grant(42, now=1001, request_key="button-1")
            self.assertEqual(store.status(42, now=1001)["expires_at"], expiry)
            self.assertEqual(store.grant(42, now=1002, request_key="button-2"), expiry + 7 * 86400)

    def test_pagination_has_stable_order_and_no_thirty_member_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Subscribers(Path(folder) / "users.db")
            for user in range(35):
                store.request(user, now=1000)
            rows = [row for offset in (0, 10, 20, 30) for row in store.member_profiles(10, offset)]
            self.assertEqual(len({r["chat_id"] for r in rows}), 35)


class RestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_flag_keeps_delivered_s1_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = CycleJournal(Path(folder) / "cycles.db")
            engine = BollingerCycles(journal, clock=lambda: 1002)
            plan = dict(symbol="TEST", entry_at=1020, expires_at=1380, cycle_seconds=120,
                        direction="UP", payout=93, model_sha256="v1")
            ident = journal.reserve(plan, 1000)
            journal.sent(ident, 1001, 7)
            engine.preserve_delivered_on_stop = True
            await engine.stop()
            self.assertEqual(len(journal.pending()), 1)
            engine.preserve_delivered_on_stop = False
            await engine.stop()
            self.assertEqual(journal.pending(), [])

    async def test_restart_preserves_delivered_plan_but_manual_cancel_reaches_subscriber(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            journal = CycleJournal(root / "data" / "bollinger-cycles.db")
            plan = dict(symbol="TEST", name="TEST OTC", entry_at=1020, expires_at=1380,
                        cycle_seconds=120, direction="UP", payout=93, model_sha256="v1")
            ident = journal.reserve(plan, 1000)
            journal.sent(ident, 1001, 7)
            journal.cancel_future(1002, preserve_sent=True)
            self.assertEqual(journal.pending()[0]["sent_at"], 1001)
            store = Subscribers(root / "data" / "subscribers.db")
            store.request(42, now=990)
            store.grant(42, now=990)
            store.saved(42, "main:" + ident, 8, "PENDING", "old")
            journal.cancel_future(1003)
            self.assertEqual(main_signals(journal.path), [])
            relay = SubscriberRelay(root, store=store, clock=lambda: 1004)
            bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock())
            await relay.update(bot)
            self.assertIn("ОТМЕНЁН", bot.edit_message_text.await_args.args[0])
            bot.send_message.assert_not_awaited()
            await relay.update(bot)
            bot.edit_message_text.assert_awaited_once()

    async def test_slow_result_maintenance_does_not_block_s1_planning(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = BollingerCycles(CycleJournal(Path(folder) / "cycles.db"), clock=lambda: 7180)
            engine.snapshot = ({}, {}, {}, None)
            engine.enabled = True
            planned = asyncio.Event()
            async def slow(*args):
                await asyncio.sleep(10)
            async def plan(*args):
                planned.set()
            engine.settle_pending = slow
            engine.plan = plan
            engine.update_cards = AsyncMock()
            task = asyncio.create_task(engine.run(SimpleNamespace(), 1))
            try:
                await asyncio.wait_for(planned.wait(), 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_slow_result_maintenance_does_not_block_secondary_publishing(self):
        from services.otc_strategies_live import LiveStrategyPublisher
        with tempfile.TemporaryDirectory() as folder:
            engine = LiveStrategyPublisher(folder)
            published = asyncio.Event()
            async def slow():
                await asyncio.sleep(10)
            async def publish(*args):
                published.set()
            engine.settle = slow
            engine.publish = publish
            task = asyncio.create_task(engine.run(SimpleNamespace(), 1))
            try:
                await asyncio.wait_for(published.wait(), 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


class DashboardAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_subscriber_cannot_open_personal_or_global_stats(self):
        for scope in ("personal", "global"):
            query = SimpleNamespace(data=f"c:cycles:main:7d:all:{scope}",
                                    from_user=SimpleNamespace(id=42), answer=AsyncMock(),
                                    message=SimpleNamespace(chat=SimpleNamespace(id=42, type="private")))
            store = SimpleNamespace(status=lambda user: {"state": "pending"})
            with patch.object(community, "is_owner", return_value=False), \
                 patch.object(community, "subscribers", return_value=store), \
                 patch.object(community, "show_cycles", new_callable=AsyncMock) as show:
                await community.callbacks(query)
                show.assert_not_awaited()
                self.assertTrue(query.answer.await_args.kwargs["show_alert"])

    async def test_callback_preserves_selected_filters_and_user_scope(self):
        query = SimpleNamespace(data="c:cycles:ema_pullback:30d:15:personal",
                                from_user=SimpleNamespace(id=42), answer=AsyncMock(),
                                message=SimpleNamespace(chat=SimpleNamespace(id=42, type="private")))
        store = SimpleNamespace(status=lambda user: {"state": "approved"})
        with patch.object(community, "is_owner", return_value=False), \
             patch.object(community, "subscribers", return_value=store), \
             patch.object(community, "show_cycles", new_callable=AsyncMock) as show:
            await community.callbacks(query)
        show.assert_awaited_once_with(query.message, source="ema_pullback", period="30d",
                                      timeframe="15", scope="personal", owner=False, edit=True)
