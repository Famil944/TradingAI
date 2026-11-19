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


class PollResultsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.polls = AnnouncementStore(self.root / "announcements.db")
        self.users = Subscribers(self.root / "subscribers.db")

    def test_choice_is_persistent_and_exactly_one_per_user(self):
        aid = self.polls.create(
            "poll", "Продолжать проект?", ["Да", "Нет", "Без разницы"], [7, 8, 9],
        )
        self.assertTrue(self.polls.vote(aid, 7, 0))
        self.assertTrue(self.polls.vote(aid, 8, 1))
        self.assertEqual(self.polls.summary(aid)["votes"], [1, 1, 0])
        self.assertTrue(self.polls.vote(aid, 7, 1))
        fresh = AnnouncementStore(self.polls.path)
        self.assertEqual(fresh.summary(aid)["votes"], [0, 2, 0])
        self.assertEqual(fresh.selected(aid, 7), 1)
        self.assertTrue(fresh.is_recipient(aid, 8))
        self.assertFalse(fresh.is_recipient(aid, 900))
        count, rows = fresh.participants(aid)
        self.assertEqual(count, 2)
        self.assertEqual([row["chat_id"] for row in rows], [7, 8])
        self.assertTrue(all(row["option_index"] == 1 for row in rows))

    def test_nonrespondents_must_have_received_message(self):
        aid = self.polls.create("poll", "Вопрос?", ["А", "Б"], [7, 8, 9])
        self.polls.delivery_state(aid, 7, "sent")
        self.polls.delivery_state(aid, 8, "sent")
        self.polls.delivery_state(aid, 9, "skipped")
        self.polls.vote(aid, 7, 0)
        total, rows = self.polls.participants(aid, "unvoted")
        self.assertEqual((total, [row["chat_id"] for row in rows]), (1, [8]))
        self.assertEqual(self.polls.summary(aid)["recipient_count"], 3)
        self.assertEqual(self.polls.participants(aid, "voted", 1, 1), (1, []))

    def test_existing_profile_lookup_does_not_duplicate_personal_data(self):
        self.users.request(7)
        self.users.record_interaction(7, "Алексей", "М", "example_7")
        self.users.request(8)
        self.users.record_interaction(8, "Мария", "", None)
        found = self.users.profiles([7, 8, 999])
        self.assertEqual(found[7]["username"], "example_7")
        self.assertEqual(found[8]["first_name"], "Мария")
        self.assertEqual(
            announcements.participant_label(7, found),
            "Алексей М (@example_7) · ID 7"
        )
        self.assertIn("Без имени", announcements.participant_label(999, found))
        self.assertEqual(self.users.profiles([]), {})

    def test_owner_text_groups_votes_but_subscriber_keyboard_has_only_percentages(self):
        aid = self.polls.create("poll", "Вопрос?", ["Да", "Нет"], [7, 8])
        self.polls.vote(aid, 7, 0)
        self.polls.vote(aid, 8, 1)
        info = self.polls.summary(aid)
        summary = announcements.summary_text(info)
        self.assertIn("Проголосовали: 2 из 2 (100% адресатов)", summary)
        self.assertIn("Да: 1 (50%)", summary)
        markup = announcements.vote_keyboard(aid, info["options"], 0, info["votes"])
        labels = [button[0].text for button in markup.inline_keyboard]
        self.assertIn("50%", labels[0])
        self.assertIn("50%", labels[1])
        self.assertIn("голосов 2", labels[-1])
        self.assertNotIn("ID 7", str(markup))
        _, rows = self.polls.participants(aid)
        owner_view = announcements.participant_text(
            info, "voted", 2, rows, {}, 0
        )
        self.assertIn("Вариант 1: Да", owner_view)
        self.assertIn("Вариант 2: Нет", owner_view)
        self.assertIn("ID 7", owner_view)
        self.assertIn("ID 8", owner_view)
        self.assertIn("a:people:", str(announcements.owner_results_keyboard(aid)))


class PollComposerTests(unittest.TestCase):
    def test_blank_lines_and_emojis_are_not_extra_answers(self):
        draft = {"kind": "poll", "phase": "options", "body": "Продолжать?", "options": []}
        self.assertEqual(announcements.append_options(
            draft,
            "\n✅ Да, продолжай!\n\n🔥 Проект интересный!\n\n"
            "🤔 Пока наблюдаю.\n\n❌ Пока нет.\n"
        ), 4)
        self.assertEqual(len(draft["options"]), 4)
        self.assertIn("4/8", announcements.options_status(draft))
        self.assertEqual(AnnouncementStore.validate(
            "poll", draft["body"], draft["options"]
        )[1], draft["options"])

    def test_separate_messages_and_edit(self):
        draft = {"kind": "poll", "phase": "options", "body": "Ваше мнение?", "options": []}
        self.assertEqual(announcements.append_options(draft, "Да"), 1)
        self.assertEqual(announcements.append_options(draft, "Нет"), 2)
        self.assertIn("✅ Готово", announcements.option_controls().inline_keyboard[0][0].text)
        self.assertEqual(draft["options"], ["Да", "Нет"])
        self.assertEqual(draft["options"].pop(), "Нет")
        self.assertEqual(announcements.append_options(draft, "Не знаю"), 2)

    def test_duplicates_and_maximum_are_explained(self):
        draft = {"kind": "poll", "phase": "options", "body": "Вопрос", "options": []}
        announcements.append_options(draft, "Да")
        with self.assertRaisesRegex(ValueError, "одинаковые"):
            announcements.append_options(draft, "\nДА\n")
        self.assertEqual(draft["options"], ["Да"])
        with self.assertRaisesRegex(ValueError, "максимум 8"):
            announcements.append_options(
                draft, "\n".join(f"Вариант {n}" for n in range(10))
            )
        self.assertEqual(len(draft["options"]), 1)


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

    async def test_step_by_step_owner_compose_and_confirmation(self):
        owner_id = announcements.settings.admin_chat_id
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=owner_id),
            chat=SimpleNamespace(id=owner_id, type="private"),
            text="Стоит продолжать?",
            answer=AsyncMock(),
        )
        announcements.drafts[owner_id] = {"kind": "poll", "phase": "body"}
        try:
            await announcements.compose(message)
            self.assertEqual(announcements.drafts[owner_id]["phase"], "options")
            message.text = "\n✅ Да\n\n🔥 Конечно\n\n🤔 Пока смотрю\n"
            await announcements.compose(message)
            self.assertEqual(len(announcements.drafts[owner_id]["options"]), 3)
            self.assertEqual(announcements.drafts[owner_id]["phase"], "options")
            self.assertIn("3/8", message.answer.await_args.args[0])
            query = SimpleNamespace(
                message=message,
                data="a:done",
                from_user=message.from_user,
                answer=AsyncMock(),
            )
            await announcements.callbacks(query)
            self.assertEqual(announcements.drafts[owner_id]["phase"], "confirm")
            self.assertIn("👁 Шаг 3", message.answer.await_args.args[0])
            self.assertEqual(
                message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
                "a:send"
            )
            message.text = "случайный текст"
            await announcements.compose(message)
            self.assertIn("Предпросмотр уже готов", message.answer.await_args.args[0])
        finally:
            announcements.drafts.pop(owner_id, None)

    async def test_done_requires_two_answers(self):
        owner_id = announcements.settings.admin_chat_id
        msg = SimpleNamespace(chat=SimpleNamespace(id=owner_id, type="private"),
                              answer=AsyncMock())
        query = SimpleNamespace(message=msg, from_user=SimpleNamespace(id=owner_id),
                                data="a:done", answer=AsyncMock())
        announcements.drafts[owner_id] = {
            "kind": "poll", "phase": "options", "body": "Вопрос", "options": ["Да"]
        }
        try:
            await announcements.callbacks(query)
            self.assertEqual(announcements.drafts[owner_id]["phase"], "options")
            self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        finally:
            announcements.drafts.pop(owner_id, None)

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

    async def test_percentages_after_vote_and_user_refresh(self):
        now = time.time()
        self.subscribers.grant(43, now=now)
        aid = self.store.create(
            "poll", "Продолжать?", ["Да", "Нет"], [42, 43],
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=42, type="private"),
            edit_reply_markup=AsyncMock(),
            answer=AsyncMock(),
        )
        query = SimpleNamespace(
            data=f"a:vote:{aid}:0",
            from_user=SimpleNamespace(id=42),
            message=message,
            answer=AsyncMock(),
        )
        await announcements.callbacks(query)
        self.assertEqual(self.store.selected(aid, 42), 0)
        keyboard = message.edit_reply_markup.await_args.kwargs["reply_markup"]
        self.assertIn("100%", keyboard.inline_keyboard[0][0].text)
        self.assertIn("0%", keyboard.inline_keyboard[1][0].text)
        self.assertEqual(self.store.summary(aid)["votes"], [1, 0])
        self.assertIn("Владелец", query.answer.await_args.args[0])

        self.store.vote(aid, 43, 1)
        query.data = f"a:refresh:{aid}"
        await announcements.callbacks(query)
        self.assertEqual(self.store.summary(aid)["votes"], [1, 1])
        keyboard = message.edit_reply_markup.await_args.kwargs["reply_markup"]
        self.assertIn("50%", keyboard.inline_keyboard[0][0].text)
        self.assertIn("50%", keyboard.inline_keyboard[1][0].text)
        self.assertEqual(self.store.selected(aid, 42), 0)

    async def test_owner_sees_voter_names_and_subscriber_cannot_list_them(self):
        aid = self.store.create("poll", "Выбор", ["За", "Против"], [42])
        self.store.vote(aid, 42, 0)
        self.subscribers.record_interaction(42, "Тест", "Участник", "vote42")
        message = SimpleNamespace(
            chat=SimpleNamespace(id=announcements.settings.admin_chat_id,
                                 type="private"),
            answer=AsyncMock(),
        )
        query = SimpleNamespace(
            data=f"a:people:{aid}:voted:0",
            from_user=SimpleNamespace(id=announcements.settings.admin_chat_id),
            message=message,
            answer=AsyncMock(),
        )
        await announcements.callbacks(query)
        text = message.answer.await_args.args[0]
        self.assertIn("Тест Участник (@vote42) · ID 42", text)
        self.assertIn("Вариант 1: За", text)
        message.answer.reset_mock()
        query.from_user.id = 42
        await announcements.callbacks(query)
        message.answer.assert_not_awaited()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

    async def test_only_recipient_may_refresh_results(self):
        aid = self.store.create("poll", "Выбор", ["За", "Против"], [42])
        self.subscribers.grant(43, now=time.time())
        message = SimpleNamespace(
            chat=SimpleNamespace(id=43, type="private"),
            edit_reply_markup=AsyncMock(),
        )
        query = SimpleNamespace(
            data=f"a:refresh:{aid}",
            from_user=SimpleNamespace(id=43),
            message=message,
            answer=AsyncMock(),
        )
        await announcements.callbacks(query)
        message.edit_reply_markup.assert_not_awaited()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

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
