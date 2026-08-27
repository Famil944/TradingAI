import unittest
from unittest.mock import patch

from services.demo_trade_monitor import DemoTradeMonitor


class FakeManager:
    def __init__(self):
        self.tp_checks = 0
        self.break_even_checks = 0

    def check_tp_sl(self, **kwargs):
        self.tp_checks += 1
        return "⏳ Позиция удерживается" if self.tp_checks == 1 else "Позиции нет."

    def check_break_even(self, **kwargs):
        self.break_even_checks += 1
        return {
            "move_stop": True,
            "new_stop_loss": 100,
            "profit_percent": 0.5,
        }

    def calculate_trailing_stop(self, **kwargs):
        return {"updated": False}

    def position_quantity(self, symbol):
        return 1

    def replace_stop_loss(self, **kwargs):
        return {"orderId": 10}

    def finalize_exchange_closed_position(self, symbol):
        return None


class DemoMonitorTests(unittest.TestCase):
    @patch("services.demo_trade_monitor.notifier.send")
    def test_break_even_updates_stop_once_and_cleans_up(self, _send):
        manager = FakeManager()
        stopped = []
        monitor = DemoTradeMonitor(
            symbol="BTCUSDT",
            entry_price=100,
            take_profit=110,
            stop_loss=95,
            side="LONG",
            interval_seconds=0,
            manager=manager,
            on_stop=lambda symbol, instance: stopped.append((symbol, instance)),
        )

        monitor.start()

        self.assertEqual(monitor.stop_loss, 100)
        self.assertTrue(monitor.break_even_activated)
        self.assertEqual(manager.break_even_checks, 1)
        self.assertEqual(stopped, [("BTCUSDT", monitor)])
