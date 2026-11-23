"""Owner menu regression checks. UI only; trading logic must stay untouched."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot import community, otc_commands


def message(user_id=700):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=user_id, type="private"),
        answer=AsyncMock(),
        text="/suggest",
    )


class OwnerMenuTests(unittest.IsolatedAsyncioTestCase):
    def test_owner_home_no_subscriber_preferences(self):
        keyboard = community.home(owner=True)
        self.assertEqual(len(keyboard.inline_keyboard), 2)
        targets = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertEqual(targets, ["c:cycles:all:today:1:global", "c:users", "c:queue", "c:owner"])
        self.assertNotIn("c:suggest", targets)

    def test_owner_panel_has_compact_navigation(self):
        with patch.object(otc_commands, "service", None):
            keyboard = otc_commands.menu(700)
        nav = [
            row for row in keyboard.inline_keyboard
            if any(button.callback_data == "c:queue" for button in row)
        ]
        self.assertEqual(len(nav), 1)
        self.assertEqual(
            [button.callback_data for button in nav[0]],
            ["c:sources", "c:users", "c:queue"]
        )

    async def test_owner_help_is_different_from_otc_menu(self):
        msg = message()
        with patch.object(community, "is_owner", return_value=True), \
             patch.object(otc_commands, "show_menu", new_callable=AsyncMock) as show:
            await community.help_menu(msg)
        show.assert_not_awaited()
        body = msg.answer.await_args.args[0]
        self.assertIn("Управление TradingAI", body)
        self.assertIn("/suggestions", body)

    async def test_owner_old_suggest_command_reads_inbox_only(self):
        msg = message()
        with patch.object(community, "is_owner", return_value=True), \
             patch.object(community, "show_queue", new_callable=AsyncMock) as queue, \
             patch.object(community, "feedback") as feedback:
            await community.suggest(msg)
        queue.assert_awaited_once_with(msg)
        feedback.assert_not_called()

    async def test_owner_old_suggest_button_reads_inbox_only(self):
        msg = message()
        query = SimpleNamespace(
            data="c:suggest", from_user=msg.from_user,
            message=msg, answer=AsyncMock(),
        )
        with patch.object(community, "is_owner", return_value=True), \
             patch.object(community, "show_queue", new_callable=AsyncMock) as queue, \
             patch.object(community, "feedback") as feedback:
            await community.callbacks(query)
        queue.assert_awaited_once_with(msg, edit=True)
        feedback.assert_not_called()

    async def test_owner_old_unfinished_suggestion_cannot_be_submitted(self):
        msg = message()
        msg.text = "Предложение от владельца"
        store = MagicMock()
        store.state.return_value = {"mode": "SUGGEST"}
        with patch.object(community, "is_owner", return_value=True), \
             patch.object(community, "feedback", return_value=store):
            await community.receive_text(msg)
        store.submit.assert_not_called()
        store.cancel.assert_called_once_with(700)
        self.assertIn("не отправляет предложения", msg.answer.await_args.args[0])

    async def test_subscriber_can_still_suggest(self):
        msg = message(800)
        status = MagicMock()
        status.status.return_value = {"state": "approved"}
        store = MagicMock()
        with patch.object(community, "is_owner", return_value=False), \
             patch.object(community, "subscribers", return_value=status), \
             patch.object(community, "feedback", return_value=store):
            await community.suggest(msg)
        store.begin.assert_called_once_with(800)
        self.assertIn("Напиши предложение", msg.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
