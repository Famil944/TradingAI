import unittest

from config.models import CandleData
from services.news_sentiment_service import NewsAssessment
from core.scan_coordinator import market_scan_lock
from services.pump_service import (
    MAX_TRACKED_CANDIDATES_PER_SCAN, PumpScanner, PumpService,
)


def candles(volume_multiplier=3, buy_ratio=0.60, trade_multiplier=3):
    result = []
    for index in range(65):
        recent = index >= 61
        close = 101 if index == 64 else 100
        result.append(CandleData(
            timestamp=index * 60_000, open=100, high=101.2 if index == 64 else 100.5,
            low=99.5, close=close,
            volume=100 * volume_multiplier if recent else 100,
            trade_count=100 * trade_multiplier if recent else 100,
            taker_buy_quote_volume=(
                100 * volume_multiplier * buy_ratio if recent else 50
            ),
        ))
    return result


class FakeNews:
    async def assess(self, symbol):
        return NewsAssessment(available=True, score=0, relevant_items=1)


class FakeClient:
    def __init__(self, five_minute_kwargs=None, **candle_kwargs):
        self.candle_kwargs = candle_kwargs
        self.five_minute_kwargs = five_minute_kwargs

    async def get_klines(self, symbol, interval, limit):
        if interval == "5m" and self.five_minute_kwargs is not None:
            return candles(**self.five_minute_kwargs)
        return candles(**self.candle_kwargs)


class BrokenBot:
    async def send_message(self, user_id, text):
        raise RuntimeError("Telegram unavailable")


class ManyCandidateScanner:
    async def scan(self, progress=None):
        return [
            {"symbol": f"TEST{index}USDT", "score": 90 - index, "price": 1}
            for index in range(10)
        ]


class RecordingPumpDatabase:
    def __init__(self):
        self.saved = []

    def save_pump_prediction(self, user_id, candidate):
        self.saved.append(candidate["symbol"])
        return len(self.saved)


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

    async def test_rejects_isolated_volume_spike_without_activity_confirmation(self):
        scanner = PumpScanner(FakeNews())
        candidate, reason = await scanner._analyze(
            FakeClient(
                volume_multiplier=2,
                trade_multiplier=1,
                five_minute_kwargs={"volume_multiplier": 1, "trade_multiplier": 1},
            ),
            {
                "symbol": "NOISYUSDT", "price": 101,
                "quote_volume": 20_000_000, "bid": 100.99, "ask": 101.01,
                "price_change_percent": 1, "trade_count": 10000,
            },
        )
        self.assertIsNone(candidate)
        self.assertEqual(reason, "pump_activity_unconfirmed")

    async def test_rejects_volume_spike_dominated_by_sellers(self):
        scanner = PumpScanner(FakeNews())
        candidate, reason = await scanner._analyze(
            FakeClient(buy_ratio=0.30),
            {
                "symbol": "SELLERSUSDT", "price": 101,
                "quote_volume": 20_000_000, "bid": 100.99, "ask": 101.01,
                "price_change_percent": 1, "trade_count": 10000,
            },
        )
        self.assertIsNone(candidate)
        self.assertEqual(reason, "pump_buyers_weak")

    async def test_only_best_candidates_are_saved_and_monitored(self):
        service = PumpService(bot=None, news_service=FakeNews())
        service.scanner = ManyCandidateScanner()
        service.db = RecordingPumpDatabase()

        candidates, saved = await service.scan_for_user(123)

        self.assertEqual(len(candidates), 10)
        self.assertEqual(len(saved), MAX_TRACKED_CANDIDATES_PER_SCAN)
        self.assertEqual(
            service.db.saved,
            ["TEST0USDT", "TEST1USDT", "TEST2USDT"],
        )


if __name__ == "__main__":
    unittest.main()
