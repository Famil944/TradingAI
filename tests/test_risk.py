import unittest
from datetime import datetime, timedelta, timezone

from services.demo_position_sizer import DemoPositionSizer
from services.trading_risk_guard import TradingRiskGuard


class PositionSizerTests(unittest.TestCase):
    def test_long_and_short_use_fixed_risk(self):
        sizer = DemoPositionSizer(risk_percent=1, max_quantity=10)
        self.assertEqual(sizer.calculate_long_quantity(1000, 100, 95), 2)
        self.assertEqual(sizer.calculate_short_quantity(1000, 100, 105), 2)

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            DemoPositionSizer(risk_percent=10)

    def test_adaptive_risk_and_balance_allocation_cap(self):
        sizer = DemoPositionSizer(
            risk_percent=1,
            max_quantity=None,
            max_balance_percent=20,
        )
        high_quality = sizer.calculate_long_quantity(
            1000, 100, 99, quality_score=90
        )
        low_quality = sizer.calculate_long_quantity(
            1000, 100, 99, quality_score=70
        )
        self.assertEqual(high_quality, 2)
        self.assertEqual(low_quality, 2)
        self.assertEqual(
            sizer.calculate_long_quantity(
                1000, 100, 95, quality_score=70
            ),
            1,
        )


class RiskGuardTests(unittest.TestCase):
    def test_accepts_valid_long_and_short(self):
        guard = TradingRiskGuard()
        self.assertTrue(guard.validate_trade("LONG", 100, 95, 110, 1))
        self.assertTrue(guard.validate_trade("SHORT", 100, 105, 90, 1))

    def test_rejects_bad_risk_reward(self):
        guard = TradingRiskGuard(min_reward_risk=2)
        with self.assertRaises(PermissionError):
            guard.validate_trade("LONG", 100, 95, 105, 1)

    def test_limits_open_positions_and_requires_balance(self):
        guard = TradingRiskGuard(max_open_positions=1)
        with self.assertRaises(PermissionError):
            guard.validate_market_state([{"symbol": "BTCUSDT"}], 100)
        with self.assertRaises(ValueError):
            guard.validate_market_state([], 0)

    def test_daily_loss_and_cooldown_stop_new_trade(self):
        now = datetime.now(timezone.utc)
        trade = {
            "symbol": "BTCUSDT",
            "status": "CLOSED",
            "pnl": -40,
            "closed_at": now.isoformat(),
        }
        guard = TradingRiskGuard(
            max_daily_loss_percent=3,
            cooldown_minutes=15,
        )
        with self.assertRaises(PermissionError):
            guard.validate_history([trade], balance=1000, now=now)

        trade["pnl"] = 1
        with self.assertRaises(PermissionError):
            guard.validate_history(
                [trade],
                balance=1000,
                symbol="BTCUSDT",
                now=now + timedelta(minutes=1),
            )
