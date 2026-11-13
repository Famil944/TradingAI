import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.otc_market import Asset
from services.otc_ml.preview import PreviewJournal
from services.otc_ready import select_ready_v5 as select_ready
from services.otc_strategy import Candle


class ReadyTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000040
        self.asset = Asset("A", True, True, 92, None, "Asset", self.now)
        self.features = dict(
            atr14=0.01,
            adx14=0.3,
            ema20_gap=0.005,
            ema_spread=0.003,
            macd_hist=0.001,
            rsi14=0.6,
            bb_position=0.7,
        )
        self.setup = SimpleNamespace(
            direction="UP", evidence=dict(features=self.features)
        )

    def test_ready_signal_uses_completed_candles_and_no_colour_condition(self):
        history = [
            Candle(1799999940, 1, 1.1, 0.9, 1),
            Candle(1800000000, 1, 999, 0.001, 500),
        ]

        def setup(bars, at, seconds):
            self.assertTrue(all(b.time + 60 <= at for b in bars))
            return self.setup

        with patch("services.otc_ready.context_setup", side_effect=setup):
            selected = select_ready(
                {"A": history}, {"A": self.asset}, {"A": self.now}, self.now, 60
            )
        self.assertEqual(selected["entry_at"], 1800000060)
        self.assertFalse(selected["condition_required"])
        self.assertEqual(selected["direction"], "UP")

    def test_conflicting_or_stretched_context_is_not_published(self):
        for changes in (dict(macd_hist=-0.001), dict(adx14=0.1), dict(ema20_gap=0.03)):
            features = dict(self.features, **changes)
            with patch(
                "services.otc_ready.context_setup",
                return_value=SimpleNamespace(
                    direction="UP", evidence=dict(features=features)
                ),
            ):
                self.assertIsNone(
                    select_ready(
                        {"A": []}, {"A": self.asset}, {"A": self.now}, self.now, 60
                    )
                )

    def test_late_or_stale_quotes_never_generate_ready_signal(self):
        self.assertIsNone(
            select_ready(
                {"A": []}, {"A": self.asset}, {"A": self.now - 9}, self.now, 60
            )
        )
        self.assertIsNone(
            select_ready(
                {"A": []}, {"A": self.asset}, {"A": self.now}, self.now + 6, 60
            )
        )

    def test_same_entry_has_independent_horizons_without_overlapping_same_expiry(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = PreviewJournal(
                Path(folder) / "ready.db", label="Primary", per_horizon=True
            )
            base = dict(
                symbol="A",
                entry_at=1800000060,
                direction="UP",
                payout=92,
                model_sha256="v5",
            )
            for seconds in (60, 300, 900):
                selected = dict(base, expires_at=base["entry_at"] + seconds)
                self.assertIsNotNone(journal.reserve(selected, self.now))
                self.assertIsNone(
                    journal.reserve(
                        dict(
                            selected,
                            entry_at=1800000120,
                            expires_at=1800000120 + seconds,
                        ),
                        self.now,
                    )
                )
            self.assertEqual(len(journal.pending()), 3)
