"""Subscriber controls for opting out of M5/M15 without changing published signals."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import community
from services.otc_subscribers import SubscriberRelay, Subscribers


def grant(store, chat_id, now=1000):
    store.request(chat_id, now=now)
    store.grant(chat_id, now=now)


def signal(sid, seconds=60, outcome="PENDING"):
    return dict(
        id=str(sid), origin="main", strategy="bollinger_live",
        label="Боллинджер ×3", seconds=seconds,
        symbol="TEST OTC", direction="UP", version="test",
        created_at=1000, sent_at=1004, entry_at=1020,
        expires_at=1020 + 3 * seconds, resolved_at=1020 + 3 * seconds,
        outcome=outcome, attempts=[],
    )


class TimeframePreferenceTests(unittest.TestCase):
    def test_defaults_persist_and_are_independent_of_strategy_preferences(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "users.db"
            store = Subscribers(path)
            self.assertEqual(store.timeframe_preferences(42),
                             {60: True, 120: True, 300: True, 900: True})
            store.set_timeframe(42, 300, False)
            store.set_timeframe(42, 900, False)
            self.assertEqual(store.timeframe_preferences(42),
                             {60: True, 120: True, 300: False, 900: False})
            self.assertTrue(store.allows_timeframe(43, 300))
            self.assertFalse(store.allows_timeframe(42, 300))
            self.assertTrue(all(store.source_preferences(42).values()))
            self.assertEqual(Subscribers(path).timeframe_preferences(42),
                             {60: True, 120: True, 300: False, 900: False})
            store.set_timeframe(42, 900, True)
            self.assertTrue(store.allows_timeframe(42, 900))
            self.assertFalse(store.allows_timeframe(42, 301))
            for seconds in (0, 90, 301, 1200, True, "300"):
                with self.assertRaises(ValueError):
                    store.set_timeframe(42, seconds, False)


class TimeframeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_now_applies_per_user_timeframes_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = Subscribers(root / "data" / "subscribers.db")
            grant(store, 42)
            grant(store, 43)
            store.set_timeframe(42, 300, False)
            store.set_timeframe(42, 900, False)
            bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
            relay = SubscriberRelay(root, store=store, clock=lambda: 1004)
            for index, seconds in enumerate((60, 120, 300, 900)):
                await relay.publish_now(bot, signal(f"new-{index}", seconds))
            sent = [call.args[0] for call in bot.send_message.await_args_list]
            self.assertEqual(sent.count(42), 1)
            self.assertEqual(sent.count(43), 1)
            self.assertTrue(store.delivered(42, "new-0"))
            self.assertIsNone(store.delivered(42, "new-1"))
            self.assertIsNone(store.delivered(42, "new-2"))
            self.assertIsNone(store.delivered(43, "new-1"))

    async def test_fallback_filters_new_messages_but_updates_prior_outcome(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = Subscribers(root / "data" / "subscribers.db")
            grant(store, 42)
            bot = SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)),
                edit_message_text=AsyncMock(),
            )
            relay = SubscriberRelay(root, store=store, clock=lambda: 1004)
            existing = signal("m2-existing", 120)
            store.saved(42, existing["id"], 5, "PENDING", "old")
            self.assertEqual(bot.send_message.await_count, 0)
            store.set_timeframe(42, 120, False)
            store.set_timeframe(42, 300, False)
            rows = [signal("m5-new", 300), signal("m2-existing", 120, "WIN")]
            relay._signals = lambda: rows
            await relay.update(bot)
            self.assertEqual(bot.send_message.await_count, 0)
            # Deliver already-recorded WIN/LOSS edits without generating new sends.
            bot.edit_message_text.assert_awaited_once()
            store.set_timeframe(42, 300, True)
            await relay.update(bot)
            self.assertEqual(bot.send_message.await_count, 0)
            self.assertIsNone(store.delivered(42, "m5-new"))


class TimeframeMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_toggle_through_personal_menu_and_denied_to_nonmembers(self):
        with tempfile.TemporaryDirectory() as d:
            store = Subscribers(Path(d) / "users.db")
            now = time.time()
            grant(store, 42, now)
            menu = community.preferences_menu(store, 42)
            labels = [b.text for row in menu.inline_keyboard for b in row]
            self.assertIn("✅ M1", labels)
            self.assertNotIn("✅ M5", labels)
            self.assertNotIn("✅ M15", labels)
            self.assertNotIn("c:tfpref:15", [b.callback_data
                                          for row in menu.inline_keyboard for b in row])
            msg = SimpleNamespace(
                chat=SimpleNamespace(id=42, type="private"),
                edit_text=AsyncMock(), answer=AsyncMock(),
            )
            query = SimpleNamespace(
                from_user=SimpleNamespace(id=42),
                message=msg, data="c:tfpref:1",
                answer=AsyncMock(),
            )
            with patch.object(community, "subscribers", return_value=store):
                await community.callbacks(query)
                self.assertFalse(store.allows_timeframe(42, 60))
                self.assertTrue(store.allows_timeframe(42, 900))
                self.assertIn("ни одной", msg.edit_text.await_args.args[0])
                await community.callbacks(query)
                self.assertTrue(store.allows_timeframe(42, 60))
                query.from_user.id = 43
                query.message.chat.id = 43
                await community.callbacks(query)
                self.assertTrue(query.answer.await_args.kwargs["show_alert"])
                self.assertTrue(store.allows_timeframe(43, 300))


if __name__ == "__main__":
    unittest.main()
