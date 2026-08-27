import asyncio
import unittest
from types import SimpleNamespace

from services.auto_signal_service import AutoSignalService
from services.news_sentiment_service import NewsAssessment


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, user_id, text, reply_markup=None):
        self.messages.append((user_id, text, reply_markup))


class FakeDatabase:
    def get_notification_user_ids(self):
        return [123]


class FakeScanner:
    async def scan_market(self, top_limit, respect_cooldown):
        signal = SimpleNamespace(
            symbol="TESTUSDT",
            score=82,
            current_price=1.0,
            entry_zone_min=0.99,
            entry_zone_max=1.01,
            targets=SimpleNamespace(tp1=1.03, tp2=1.05),
            stop_loss=0.97,
            risk_reward=1.67,
        )
        return [{"signal_id": 7, "signal_object": signal}]


class FakeNewsService:
    async def assess(self, symbol):
        return NewsAssessment(available=True, score=20, relevant_items=2)


class AutoSignalServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_strong_signal_is_sent_with_watch_button(self):
        bot = FakeBot()
        service = AutoSignalService(
            bot, FakeScanner(), asyncio.Lock(), FakeNewsService()
        )
        service.db = FakeDatabase()

        await service.scan_and_notify()

        self.assertEqual(len(bot.messages), 1)
        user_id, text, keyboard = bot.messages[0]
        self.assertEqual(user_id, 123)
        self.assertIn("TESTUSDT", text)
        self.assertIn("ПОКУПКА", text)
        self.assertEqual(
            keyboard.inline_keyboard[0][0].callback_data, "auto_take:7"
        )


if __name__ == "__main__":
    unittest.main()
