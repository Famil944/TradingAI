"""Regression tests for owner messages and fully customizable subscriber polls."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import announcements
from services.otc_announcements import AnnouncementStore
from services.otc_subscribers import Subscribers


class AnnouncementStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AnnouncementStore(Path(self.temp.name) / "announcements.db")

    def test_owner_can_supply_any_distinct_option_labels(self):
        aid = self.store.create(
            "poll", "Какие улучшения нужны?", ["Больше инструментов", "Изменить меню", "Другое"],
            [42, 43, 42],
        )
        self.assertEqual(self.store.get(aid)["options"], [
            "Больше инструментов", "Изменить меню", "Другое"
        ])
        self.assertEqual(self.store.pending(aid), [42, 43])
        self.assertTrue(self.store.vote(aid, 42, 2))
        self.assertTrue(self.store.vote(aid, 43, 1))
        self.assertTrue(self.store.vote(aid, 42, 0))  # Changing a choice replaces a vote.
        self.assertEqual(self.store.summary(aid)["votes"], [1, 1, 0])
        self.assertEqual(self.store.selected(aid, 42), 0)
        self.assertFalse(self.store.vote(aid, 999, 0))  # Not a recipient.
        self.assertFalse(self.store.vote(aid, 42, 3))  # No such option.

    def test_validation_and_announcements_have_no_votes(self):
        for options in (["Yes"], ["x", "x"], ["", "OK"], ["x"] * 9):
            with self.assertRaises(ValueError):
                self.store.create("poll", "Question", options, [42])
        with self.assertRaises(ValueError):
            self.store.create("notice", "", [], [42])
        with self.assertRaises(ValueError):
            self.store.create("poll", "Question", ["One", "Two"], [])
        aid = self.store.create("notice", "Перерыв в работе", [], [42])
        self.assertFalse(self.store.vote(aid, 42, 0))
        self.assertEqual(self.store.summary(aid)["votes"], [])

    def test_persistent_delivery_queue_and_manual_retry(self):
        aid = self.store.create("poll", "Выбор", ["Первый", "Второй"], [42, 43])
        self.assertIn(aid, AnnouncementStore(self.store.path).pending_ids())
        self.assertTrue(self.store.delivery_state(aid, 42, "sent"))
        self.assertFalse(self.store.delivery_state(aid, 42, "sent"))
        self.assertTrue(self.store.delivery_state(aid, 43, "failed"))
        self.assertEqual(self.store.pending_ids(), [])
        self.assertEqual(self.store.retry_failed(aid), 1)
        self.assertEqual(self.store.pending(aid), [43])
        self.assertEqual(self.store.summary(aid)["delivery"], {"sent": 1, "pending": 1})


class AnnouncementFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = AnnouncementStore(root / "announcements.db")
        self.subscribers = Subscribers(root / "subscribers.db")
        now = time.time()
        self.subscribers.request(42, now=now - 60)
        self.subscribers.grant(42, now=now - 59)
        self.subscribers.request(43, now=now - 60)  # Not approved.
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)))
        self.p1 = patch.object(announcements, "store", return_value=self.store)
        self.p2 = patch.object(announcements, "subscribers", return_value=self.subscribers)
        self.p1.start()
        self.p2.start()
        self.addCleanup(self.p1.stop)
        self.addCleanup(self.p2.stop)

    async def test_only_approved_users_receive_even_without_hourly_session(self):
        self.assertEqual(await announcements.active_ids(), [42])
        aid = self.store.create("poll", "Что улучшить?", ["Скорость", "Статистику"], [42, 43])
        await announcements.deliver(self.bot, aid)
        self.bot.send_message.assert_awaited()
        delivered_ids = [call.args[0] for call in self.bot.send_message.await_args_list]
        self.assertIn(42, delivered_ids)
        self.assertNotIn(43, delivered_ids)
        self.assertIn(announcements.settings.admin_chat_id, delivered_ids)
        self.assertEqual(self.store.summary(aid)["delivery"], {"sent": 1, "skipped": 1})

    async def test_vote_changes_and_recipient_controls(self):
        aid = self.store.create("poll", "Какой таймфрейм?", ["M2", "M5", "M15"], [42])
        self.assertTrue(self.store.vote(aid, 42, 0))
        self.assertTrue(self.store.vote(aid, 42, 2))
        markup = announcements.vote_keyboard(aid, ["M2", "M5", "M15"], 2)
        self.assertEqual(markup.inline_keyboard[2][0].callback_data, f"a:vote:{aid}:2")
        self.assertIn("✅", markup.inline_keyboard[2][0].text)
        self.assertEqual(self.store.summary(aid)["votes"], [0, 0, 1])

    async def test_owner_only_compose(self):
        owner_id = announcements.settings.admin_chat_id
        message = SimpleNamespace(chat=SimpleNamespace(id=owner_id, type="private"))
        self.assertTrue(announcements.owner(SimpleNamespace(id=owner_id), message))
        self.assertFalse(announcements.owner(SimpleNamespace(id=42), message))
        self.assertFalse(announcements.owner(SimpleNamespace(id=owner_id),
                                            SimpleNamespace(chat=SimpleNamespace(
                                                id=owner_id, type="group"))))


if __name__ == "__main__":
    unittest.main()
