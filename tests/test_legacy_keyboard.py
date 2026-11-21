"""Ensure old Telegram reply keyboard can be cleared without touching trading or owner menus."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import ReplyKeyboardRemove
from bot import community


class LegacyKeyboardTests(unittest.IsolatedAsyncioTestCase):
    def message(self, chat_type="private"):
        return SimpleNamespace(
            chat=SimpleNamespace(type=chat_type, id=1234),
            from_user=SimpleNamespace(id=1234),
            answer=AsyncMock(),
        )

    async def test_clear_command_removes_old_reply_keyboard(self):
        msg = self.message()
        await community.clear_keyboard(msg)
        msg.answer.assert_awaited_once()
        self.assertIsInstance(msg.answer.await_args.kwargs["reply_markup"], ReplyKeyboardRemove)

    async def test_owner_start_removes_reply_keyboard_then_shows_inline_menu(self):
        msg = self.message()
        with patch.object(community, "is_owner", return_value=True):
            with patch.object(community.otc_commands, "show_menu", new_callable=AsyncMock) as menu:
                await community.start(msg)
        self.assertIsInstance(msg.answer.await_args.kwargs["reply_markup"], ReplyKeyboardRemove)
        menu.assert_awaited_once_with(msg)

    async def test_group_cleanup_command_is_ignored(self):
        msg = self.message(chat_type="group")
        await community.clear_keyboard(msg)
        msg.answer.assert_not_awaited()

    async def test_approved_subscriber_gets_existing_inline_menu(self):
        msg = self.message()
        fake_store = SimpleNamespace(
            status=lambda chat_id: {"state": "approved", "expires_at": 9999999999},
            session_status=lambda chat_id: {"active": False, "expires_at": None},
            source_preferences=lambda chat_id: {"main": True},
            timeframe_preferences=lambda chat_id: {120: True},
        )
        with patch.object(community, "is_owner", return_value=False):
            with patch.object(community, "subscribers", return_value=fake_store):
                await community.start(msg)
        self.assertEqual(msg.answer.await_count, 2)
        first, second = msg.answer.await_args_list
        self.assertIsInstance(first.kwargs["reply_markup"], ReplyKeyboardRemove)
        self.assertIsNotNone(second.kwargs["reply_markup"])


if __name__ == "__main__":
    unittest.main()
