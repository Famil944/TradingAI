"""Subscriber-facing instructions are clear, safe, and disclose no feed supplier."""
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from bot import community
from services.otc_public_broadcast import public_text, subscriber_card


def sample_row():
    entry = datetime(2026, 11, 18, 18, 0, tzinfo=ZoneInfo("Europe/Moscow")).timestamp()
    return dict(
        id="live:example", origin="live", strategy="bb_reentry",
        label="PRIVATE METHOD", symbol="Example OTC", direction="DOWN",
        seconds=120, entry_at=entry, outcome="PENDING",
        attempts=["LOSS"],
        provenance="INTERNAL_SOURCE", wallet="HIDDEN_ACCOUNT",
    )


class SubscriberHelpTests(unittest.TestCase):
    def test_explains_method_labels_timeframes_directions(self):
        self.assertIn("пяти", community.GUIDE_CODES)
        self.assertIn("M1 = 1 минута", community.GUIDE_CODES)
        self.assertNotIn("M5 = 5 минут", community.GUIDE_CODES)
        self.assertNotIn("M15 = 15 минут", community.GUIDE_CODES)
        self.assertIn("ВВЕРХ", community.INFO)
        self.assertIn("ВНИЗ", community.INFO)
        self.assertIn("времени", community.GUIDE_TRADE)
        self.assertIn("только после", community.GUIDE_TRADE)

    def test_public_copy_does_not_reveal_provider_or_promise_profit(self):
        row = sample_row()
        texts = [
            community.TERMS, community.INFO, community.GUIDE_TRADE,
            community.GUIDE_CODES, community.GUIDE_ACCESS,
            subscriber_card(row), public_text(row),
        ]
        for body in texts:
            self.assertNotIn("OTCharts", body)
            self.assertNotIn("PRIVATE METHOD", body)
            self.assertNotIn("INTERNAL_SOURCE", body)
            self.assertNotIn("HIDDEN_ACCOUNT", body)
            self.assertNotIn("гарантирована прибыль", body)
        self.assertIn("риском", community.GUIDE_ACCESS)
        self.assertIn("риск", community.TERMS)

    def test_subscriber_card_shows_entry_time_expiry_and_attempt_slots(self):
        card = subscriber_card(sample_row())
        self.assertIn("S2", card)
        self.assertIn("M2", card)
        self.assertIn("🔴 ВНИЗ", card)
        self.assertIn("18:00", card)
        self.assertIn("2) 18:02", card)
        self.assertIn("3) 18:04", card)
        self.assertIn("1❌", card)
        self.assertIn("2⏳", card)
        self.assertIn("3—", card)
        self.assertIn("Итог: ⏳ PENDING", card)

    def test_help_menu_has_three_information_sections(self):
        menu = community.guide_menu()
        keys = [
            b.callback_data for row in menu.inline_keyboard for b in row
        ]
        self.assertEqual(keys, [
            "c:guide:trade", "c:guide:codes", "c:guide:access", "c:home",
        ])


class HelpCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscriber_help_command_uses_guide_menu(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(id=50, type="private"),
            from_user=SimpleNamespace(id=50),
            answer=AsyncMock(), edit_text=AsyncMock(),
        )
        with patch.object(community, "is_owner", return_value=False):
            await community.help_menu(message)
        self.assertIn("TRADINGAI", message.answer.await_args.args[0])
        keyboard = message.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(
            keyboard.inline_keyboard[0][0].callback_data, "c:guide:trade"
        )

    async def test_guide_callback_works_without_subscription(self):
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=50, type="private"),
            answer=AsyncMock(), edit_text=AsyncMock(),
        )
        query = SimpleNamespace(
            data="c:guide:trade", from_user=SimpleNamespace(id=50),
            message=msg, answer=AsyncMock(), edit_text=AsyncMock(),
        )
        with patch.object(community, "is_owner", return_value=False):
            await community.callbacks(query)
        query.answer.assert_awaited_once()
        self.assertIn("КАК ЧИТАТЬ", msg.edit_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
