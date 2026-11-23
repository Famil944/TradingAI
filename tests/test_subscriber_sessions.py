"""Hourly opt-in subscriber sessions; no trading-strategy or owner path modifications."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from bot import community
from services.otc_subscribers import SubscriberRelay, Subscribers


MSK = ZoneInfo("Europe/Moscow")


def at(hour=10, minute=0):
    return datetime(2026, 11, 18, hour, minute, tzinfo=MSK).timestamp()


def approved(store, now):
    store.request(42, now=now - 10)
    store.grant(42, now=now - 10)


def live_signal(index, sent_at):
    return dict(
        id=f"live-{index}", origin="live", strategy="bb_reentry",
        label="Test private", version="v1", seconds=60,
        symbol="TEST OTC", direction="UP", created_at=sent_at - 5,
        sent_at=sent_at, entry_at=sent_at + 20, expires_at=sent_at + 390,
        resolved_at=sent_at + 390, outcome="PENDING", attempts=[],
    )


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "subscribers.db"
        self.store = Subscribers(self.path)

    def test_no_session_without_approved_access(self):
        t = at()
        with self.assertRaises(ValueError):
            self.store.start_session(42, now=t)
        self.store.request(42, now=t)
        with self.assertRaises(ValueError):
            self.store.start_session(42, now=t + 1)
        self.store.grant(42, now=t + 2)
        end = self.store.start_session(42, now=t + 3)
        self.assertEqual(end, t + 3603)
        self.assertTrue(self.store.session_allows_signal(42, t + 10, t + 20))
        self.assertFalse(self.store.session_allows_signal(42, t + 1, t + 20))
        self.assertFalse(self.store.session_allows_signal(42, t + 20, end))
        self.assertFalse(self.store.session_status(42, now=end)["active"])

    def test_renewal_restart_and_user_stop(self):
        t = at()
        approved(self.store, t)
        first_end = self.store.start_session(42, now=t)
        self.assertEqual(first_end, t + 3600)
        renewed = self.store.start_session(42, now=t + 1200)
        self.assertEqual(renewed, t + 4800)
        reloaded = Subscribers(self.path)
        self.assertTrue(reloaded.session_status(42, now=t + 1300)["active"])
        self.assertTrue(reloaded.session_allows_signal(42, t + 10, t + 1300))
        self.assertTrue(reloaded.stop_session(42, now=t + 1400))
        self.assertFalse(reloaded.session_status(42, now=t + 1401)["active"])
        self.assertEqual(reloaded.status(42, now=t + 1401)["state"], "approved")

    def test_midnight_resets_even_if_hour_not_elapsed(self):
        t = at(23, 40)
        approved(self.store, t)
        midnight = datetime(2026, 11, 19, tzinfo=MSK).timestamp()
        self.assertEqual(self.store.start_session(42, now=t), midnight)
        self.assertFalse(self.store.session_status(42, now=midnight)["active"])
        self.assertEqual(self.store.start_session(42, now=midnight + 5), midnight + 3605)

    def test_session_reminder_once_and_new_renewal(self):
        t = at()
        approved(self.store, t)
        end = self.store.start_session(42, now=t)
        self.assertEqual(self.store.session_reminders_due(now=t + 2900), [])
        self.assertEqual(self.store.session_reminders_due(now=t + 3000), [(42, end)])
        self.store.mark_session_reminded(42, end, now=t + 3000)
        self.assertEqual(self.store.session_reminders_due(now=t + 3010), [])
        self.store.start_session(42, now=t + 3200)
        self.assertEqual(self.store.session_reminders_due(now=t + 3210), [])

    def test_profile_activity_is_not_fake_reading_time(self):
        t = at()
        self.assertFalse(self.store.record_interaction(42, "Unconsented", now=t))
        approved(self.store, t)
        self.store.record_interaction(42, "Илья", "Фамилия", "ilya_42", now=t)
        self.store.record_interaction(42, "Илья", "Фамилия", "ilya_42", now=t+4)
        self.assertEqual(self.store.member_activity(42, now=t+5)["actions_7d"], 2)
        self.assertEqual(self.store.member_activity(42, now=t+5)["days_7d"], 1)
        self.assertEqual(self.store.member_profiles()[0]["first_name"], "Илья")


class SessionRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_new_signals_until_start_and_no_backfill_after_expiry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Subscribers(root / "data" / "subscribers.db")
            t = at()
            approved(store, t)
            now = [t + 10]
            relay = SubscriberRelay(root, store=store, clock=lambda: now[0], require_sessions=True)
            class Bot:
                def __init__(self):
                    self.sent = []
                async def send_message(self, chat_id, text, parse_mode=None, **kwargs):
                    self.sent.append((chat_id, text))
                    return SimpleNamespace(message_id=len(self.sent))
            bot = Bot()
            await relay.publish_now(bot, live_signal(1, t + 8))
            self.assertEqual(bot.sent, [])
            store.start_session(42, now=t + 10)
            now[0] = t + 20
            await relay.publish_now(bot, live_signal(2, t + 8))
            self.assertEqual(bot.sent, [])  # pre-session signal never backfilled
            await relay.publish_now(bot, live_signal(3, t + 19))
            self.assertEqual(len(bot.sent), 1)
            now[0] = t + 3621
            await relay.publish_now(bot, live_signal(4, t + 3620))
            self.assertEqual(len(bot.sent), 1)
            store.start_session(42, now=now[0])
            await relay.publish_now(bot, live_signal(5, t + 3620))
            self.assertEqual(len(bot.sent), 1)
            now[0] += 2
            await relay.publish_now(bot, live_signal(6, now[0] - 1))
            self.assertEqual(len(bot.sent), 2)

    async def test_owner_profile_list_only_uses_opted_in_updates(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Subscribers(Path(folder) / "users.db")
            t = at()
            approved(store, t)
            store.record_interaction(42, "Тест <User>", "", "tester42", now=t)
            message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
            with patch.object(community, "subscribers", return_value=store):
                await community.show_access(message)
                await community.show_user(message, 42)
            output = "\n".join(str(c.args[0]) for c in message.answer.call_args_list + message.edit_text.call_args_list)
            self.assertIn("Тест &lt;User&gt;", output)
            self.assertIn("https://t.me/tester42", output)
            self.assertIn("Действия / 7д: 1 · сигналы: 0", output)
            self.assertIn("Был в боте:", output)


if __name__ == "__main__":
    unittest.main()
