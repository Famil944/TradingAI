import unittest

from config.models import CandleData
from services.news_sentiment_service import NewsAssessment
from core.scan_coordinator import market_scan_lock
from services.pump_service import PumpScanner, PumpService


def candles():
    result = []
    for index in range(65):
        recent = index >= 61
        close = 101 if index == 64 else 100
        result.append(CandleData(
            timestamp=index * 60_000, open=100, high=101.2 if index == 64 else 100.5,
            low=99.5, close=close, volume=300 if recent else 100,
            trade_count=300 if recent else 100,
            taker_buy_quote_volume=180 if recent else 50,
        ))
    return result


class FakeNews:
    async def assess(self, symbol):
        return NewsAssessment(available=True, score=0, relevant_items=1)


class FakeClient:
    async def get_klines(self, symbol, interval, limit):
        return candles()


class BrokenBot:
    async def send_message(self, user_id, text):
        raise RuntimeError("Telegram unavailable")


class PumpServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_pump_and_main_scans_use_shared_lock(self):
        service = PumpService(bot=None, news_service=FakeNews())
        self.assertIs(service.lock, market_scan_lock)

    async def test_notification_failure_does_not_escape_service(self):
        service = PumpService(bot=BrokenBot(), news_service=FakeNews())
        self.assertFalse(await service._send_message_safe(123, "test"))

    def test_relative_activity_ratio(self):
        self.assertGreaterEqual(PumpScanner._ratio(candles()), 2.5)

    async def test_candidate_is_created_from_volume_and_breakout(self):
        scanner = PumpScanner(FakeNews())
        candidate, reason = await scanner._analyze(FakeClient(), {
            "symbol": "TESTUSDT", "price": 101, "quote_volume": 20_000_000,
            "bid": 100.99, "ask": 101.01, "price_change_percent": 1,
            "trade_count": 10000,
        })
        self.assertEqual(reason, "candidate")
        self.assertIsNotNone(candidate)
        self.assertGreaterEqual(candidate["score"], 60)
        self.assertIn(candidate["stage"], {"impulse", "confirmed"})


if __name__ == "__main__":
    unittest.main()
