import tempfile
import unittest
from pathlib import Path

from services.otc_shadow_variants import (
    extended_history,
    higher_trend,
    pattern_directions,
)
from services.otc_strategy import Candle


def candidate(seconds=60):
    return dict(
        version="base",
        symbol="A",
        entry_at=1800,
        expires_at=1800 + seconds,
        feature_asof=1740,
        observed_at=1780,
        direction="UP",
        payout=90,
        reason="eligible",
        gates={"min_confirmation": True},
        selected=True,
    )


class VariantTests(unittest.TestCase):
    def test_hourly_requires_30_complete_hours_and_archive_has_no_future(self):
        from services.otc_market_archive import MarketArchive

        bars = [
            Candle(
                i * 60, 100 + i * 0.01, 101 + i * 0.01, 99 + i * 0.01, 100.5 + i * 0.01
            )
            for i in range(1800)
        ]
        self.assertTrue(higher_trend(bars, 3600, 108000)["available"])
        self.assertFalse(higher_trend(bars[-1200:], 3600, 108000)["available"])
        self.assertEqual(
            higher_trend(bars, 3600, 108000),
            higher_trend(bars + [Candle(108000, 1, 99, 1, 98)], 3600, 108000),
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "archive.db"
            archive = MarketArchive(path)
            archive.bars("A", bars[:600], 107000)
            archive.bars("A", bars[600:601], 109000)
            row = dict(candidate(900), feature_asof=108000, observed_at=108040)
            extended = extended_history(bars[-1200:], path, row)
            self.assertEqual(len(extended), 1800)
            self.assertEqual(extended_history([], path, row), bars[:600])

    def test_m30_uses_complete_closed_history_without_future(self):
        bars = [
            Candle(
                i * 60, 100 + i * 0.01, 101 + i * 0.01, 99 + i * 0.01, 100.5 + i * 0.01
            )
            for i in range(930)
        ]
        asof = 900 * 60
        before = higher_trend(bars, 1800, asof)
        self.assertTrue(before["available"])
        self.assertEqual(before["state"], "UP")
        changed = bars[:900] + [Candle(900 * 60, 100, 1000, 1, 900)]
        self.assertEqual(before, higher_trend(changed, 1800, asof))
        self.assertFalse(higher_trend(bars[:899], 1800, asof)["available"])
        self.assertFalse(higher_trend(bars[:450] + bars[451:], 1800, asof)["available"])


class PatternTests(unittest.TestCase):
    def test_active_winrate_excludes_draws_unknown_and_archive(self):
        from services.otc_shadow_report import (
            format_legacy_rates as format_active_rates,
        )
        from services.otc_shadow_variants import VERSION

        item = dict(
            study_version=VERSION,
            seconds=120,
            policy="bollinger_reversal",
            selected=dict(outcomes={"WIN": 3, "LOSS": 2, "DRAW": 1}),
            selected_pending=4,
            selected_unknown=5,
        )
        text = format_active_rates(
            {"filters": [item, dict(item, study_version="old")]}, 120
        )
        self.assertIn("WR 60.0%", text)
        self.assertIn("WIN 3 / LOSS 2 / ничьи 1", text)
        self.assertIn("Ожидают 4 · нет результата 5", text)

    def test_bollinger_uses_closed_m5_and_m15_without_alternation(self):
        for period in (300, 900):
            minutes = []
            for i in range(20):
                close = 100 + (0.5 if i % 2 == 0 else -0.5)
                for offset in range(0, period, 60):
                    minutes.append(
                        Candle(
                            i * period + offset,
                            100,
                            102 if i == 19 else 100.5,
                            99.5,
                            close,
                        )
                    )
            result = pattern_directions(minutes, period, 20 * period)
            self.assertEqual(result["bollinger_reversal"]["direction"], "DOWN")
            self.assertNotIn("alternating_four", result)

    @staticmethod
    def bars():
        return [
            Candle(i * 60, 100, 101, 99, 100 + (0.5 if i % 2 == 0 else -0.5))
            for i in range(20)
        ]

    def test_alternation_both_directions_and_no_doji_or_future(self):
        bars = self.bars()
        result = pattern_directions(bars, 120, 1200)
        self.assertEqual(result["alternating_four"]["direction"], "UP")
        self.assertEqual(
            result, pattern_directions(bars + [Candle(1200, 1, 3, 1, 2)], 120, 1200)
        )
        inverted = [Candle(b.time, b.open, b.high, b.low, 200 - b.close) for b in bars]
        self.assertEqual(
            pattern_directions(inverted, 120, 1200)["alternating_four"]["direction"],
            "DOWN",
        )
        bars[-1] = Candle(1140, 100, 101, 99, 100)
        self.assertNotIn("alternating_four", pattern_directions(bars, 120, 1200))

    def test_band_touch_direction_and_ambiguous_touch(self):
        bars = self.bars()
        bars[-1] = Candle(1140, 100, 102, 99.5, 99.5)
        self.assertEqual(
            pattern_directions(bars, 120, 1200)["bollinger_reversal"]["direction"],
            "DOWN",
        )
        bars[-1] = Candle(1140, 100, 100.5, 98, 99.5)
        self.assertEqual(
            pattern_directions(bars, 120, 1200)["bollinger_reversal"]["direction"], "UP"
        )
        bars[-1] = Candle(1140, 100, 102, 98, 99.5)
        self.assertNotIn("bollinger_reversal", pattern_directions(bars, 120, 1200))

    def test_gap_stale_history_and_invalid_ohlc_abstain(self):
        bars = self.bars()
        self.assertEqual(pattern_directions(bars[:10] + bars[11:], 120, 1200), {})
        self.assertEqual(pattern_directions(bars, 120, 1260), {})
        bars[-1] = Candle(1140, 100, 99, 101, 100)
        self.assertEqual(pattern_directions(bars, 120, 1200), {})
