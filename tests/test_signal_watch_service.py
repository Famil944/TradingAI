import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from services.signal_watch_service import SignalWatchService


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, user_id, text, reply_markup=None):
        self.messages.append((user_id, text, reply_markup))


class FakeDatabase:
    def __init__(self):
        self.updates = []

    def update_manual_trade(self, trade_id, **fields):
        self.updates.append((trade_id, fields))

    def set_trade_pending(self, trade_id, reason, price, detected_at):
        self.updates.append((trade_id, {
            "status": "pending_close", "pending_reason": reason,
            "pending_price": price, "pending_at": detected_at,
        }))


class FakeClient:
    def __init__(self, candles, price=100):
        self.candles = candles
        self.price = price

    async def get_ticker(self, symbol):
        return {"price": self.price}

    async def get_klines(self, symbol, interval, limit=100):
        return self.candles


def trade(last_checked):
    return {
        "id": 1, "user_id": 123, "symbol": "TESTUSDT", "entry_price": 100,
        "current_price": 100, "tp1": 103, "stop_loss": 97,
        "max_price": 100, "min_price": 100, "tp1_hit": 0,
        "critical_alerted": 0, "last_checked_at": last_checked,
    }


class SignalWatchServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_uses_first_candle_event_not_period_extremes(self):
        now = datetime.now(timezone.utc)
        candles = [
            SimpleNamespace(timestamp=int((now - timedelta(hours=2)).timestamp() * 1000),
                            high=104, low=99),
            SimpleNamespace(timestamp=int((now - timedelta(hours=1)).timestamp() * 1000),
                            high=101, low=96),
        ]
        bot = FakeBot()
        service = SignalWatchService(bot)
        service.db = FakeDatabase()
        await service._check_manual_trade(
            FakeClient(candles),
            trade((now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")),
        )
        update = service.db.updates[-1][1]
        self.assertEqual(update["pending_reason"], "TP +3%")
        self.assertEqual(update["pending_price"], 103)

    async def test_same_candle_uses_conservative_stop(self):
        now = datetime.now(timezone.utc)
        candle = SimpleNamespace(
            timestamp=int((now - timedelta(hours=1)).timestamp() * 1000),
            high=104, low=96,
        )
        service = SignalWatchService(FakeBot())
        service.db = FakeDatabase()
        await service._check_manual_trade(
            FakeClient([candle]),
            trade((now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")),
        )
        self.assertEqual(service.db.updates[-1][1]["pending_reason"], "STOP")


if __name__ == "__main__":
    unittest.main()
