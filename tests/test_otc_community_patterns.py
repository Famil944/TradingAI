import unittest
from unittest.mock import patch

from services.otc_community_patterns import detect, forecasts
from services.otc_ml.features import ema_series
from services.otc_shadow_report import format_active_rates
from services.otc_shadow_variants import VERSION, prepare
from services.otc_strategy import Candle


class CommunityPatternTests(unittest.TestCase):
    def bars(self, prices):
        return [
            Candle(i * 60, p - 0.05, p + 0.1, p - 0.1, p) for i, p in enumerate(prices)
        ]

    def test_ema_and_mirrored_direction(self):
        prices = [100 + i * 0.1 for i in range(80)]
        bars = self.bars(prices)
        p = prices[-1]
        bars[-1] = Candle(
            79 * 60, p - 0.1, p + 0.1, ema_series(prices, 9)[-1] - 0.01, p
        )
        self.assertEqual(detect(bars)["ema_pullback"], "UP")
        mirror = [
            Candle(b.time, 300 - b.open, 300 - b.low, 300 - b.high, 300 - b.close)
            for b in bars
        ]
        self.assertEqual(detect(mirror)["ema_pullback"], "DOWN")
        self.assertEqual(
            forecasts(bars, 120, 4800),
            forecasts(bars + [Candle(4800, 1, 3, 1, 2)], 120, 4800),
        )
        self.assertEqual(forecasts(bars, 120, 4860), {})
        self.assertEqual(forecasts(bars[:30] + bars[31:], 120, 4800), {})

    def test_breakout_level_is_before_break_and_retest(self):
        bars = self.bars(
            [100 + (0.5 if i % 2 else -0.5) for i in range(78)] + [103, 102]
        )
        bars[-1] = Candle(79 * 60, 101, 102.1, 100.6, 102)
        self.assertEqual(detect(bars)["break_retest"], "UP")
        bars[-1] = Candle(79 * 60, 102, 103, 101.5, 102.5)
        self.assertNotIn("break_retest", detect(bars))

    def test_bb_requires_reentry_and_rsi_cross(self):
        prices = [100 + (0.1 if i % 2 else -0.1) for i in range(75)] + [
            99,
            98,
            97,
            96,
            100,
        ]
        self.assertEqual(detect(self.bars(prices))["bb_rsi_reentry"], "UP")
        prices[-1] = 95
        self.assertNotIn("bb_rsi_reentry", detect(self.bars(prices)))

    def test_forecasts_are_separate_shadow_rows_and_rates(self):
        row = dict(
            version="base",
            symbol="A",
            entry_at=4800,
            expires_at=4920,
            feature_asof=4740,
            observed_at=4780,
            direction="DOWN",
            selected=True,
            reason="eligible",
            gates={"min_confirmation": True},
        )
        with patch(
            "services.otc_community_patterns.forecasts",
            return_value={"ema_pullback": {"direction": "UP"}},
        ):
            rows = prepare([row], {})
        self.assertEqual(rows[0]["direction"], "DOWN")
        self.assertEqual(rows[1]["direction"], "UP")
        self.assertFalse(rows[1]["selected"])
        data = {
            "filters": [
                dict(
                    study_version=VERSION,
                    seconds=120,
                    policy="ema_pullback",
                    selected=dict(outcomes={"WIN": 3, "LOSS": 2, "DRAW": 1}),
                    selected_pending=4,
                    selected_unknown=5,
                )
            ]
        }
        text = format_active_rates(data, 120)
        self.assertIn("Откат EMA9/19: WIN 3 / LOSS 2 · WR 60.0%", text)
        self.assertIn("Ничьи 1 · ожидают 4 · нет результата 5", text)
