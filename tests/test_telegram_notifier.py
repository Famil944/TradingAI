import unittest

from services.telegram_notifier import TelegramNotifier


class TelegramNotifierTests(unittest.TestCase):
    def test_only_completed_trade_events_are_allowed(self):
        allowed = (
            "🚀 DEMO-сделка открыта",
            "🔔 Demo-сделка закрыта",
            "🏁 Позиция закрыта биржевым ордером",
        )
        blocked = (
            "❌ Ошибка AutoLoop",
            "🟡 Сигнал не прошёл фильтр",
            "📈 Trailing Stop обновлён",
            "🟢 Stop Loss перенесён в безубыток",
        )
        for text in allowed:
            self.assertTrue(TelegramNotifier._is_trade_event(text))
        for text in blocked:
            self.assertFalse(TelegramNotifier._is_trade_event(text))
