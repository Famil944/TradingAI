import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import patch

from services.otc_market import Asset
from services.otc_ml.preview import select_forecast
from services.otc_selection_quality import primary_rejection


class StrictSelectionTests(unittest.TestCase):
    def test_m5_filter_is_directional_and_requires_context(self):
        evidence = dict(
            confirmations=dict(
                checks=dict(ema="agree", candle="agree"),
                market_context=dict(available=True, range_position=0.5),
            ),
            features=dict(atr14=0.01, range_atr=1.5, ema20_gap=0.01),
        )
        self.assertIsNone(primary_rejection(evidence, 300, "UP"))
        evidence["features"]["range_atr"] = 1.9
        self.assertIsNotNone(primary_rejection(evidence, 300, "UP"))
        self.assertIsNone(primary_rejection(evidence, 120, "UP"))
        evidence["features"]["range_atr"] = 1.5
        evidence["confirmations"]["market_context"]["range_position"] = 0.9
        self.assertIsNotNone(primary_rejection(evidence, 300, "UP"))
        self.assertIsNone(primary_rejection(evidence, 300, "DOWN"))
        evidence["confirmations"]["market_context"] = {}
        self.assertIsNotNone(primary_rejection(evidence, 300, "DOWN"))

    @patch("services.otc_ml.preview.build", return_value=dict(available=5, count=1))
    def test_boost_margin_accounts_for_payout_and_both_directions(self, build):
        now = 1240
        for probabilities in ([0.46, 0, 0.54], [0.54, 0, 0.46]):
            runtime = SimpleNamespace(
                inspect=lambda *args: dict(
                    probabilities=probabilities, model_sha256="test"
                )
            )
            asset = Asset("A", True, True, 90, None, "Asset", now)
            selected = select_forecast(
                runtime, {"A": []}, {"A": asset}, {"A": now}, now
            )
            self.assertIsNotNone(selected)
            self.assertEqual(
                selected["selection_policy"], "xgboost-payout-ev-margin-v2"
            )
        runtime = SimpleNamespace(
            inspect=lambda *args: dict(
                probabilities=[0.47, 0, 0.53], model_sha256="test"
            )
        )
        reasons = Counter()
        self.assertIsNone(
            select_forecast(
                runtime, {"A": []}, {"A": asset}, {"A": now}, now, reasons=reasons
            )
        )
        self.assertEqual(reasons["model_ev_below_margin"], 1)
        asset = Asset("A", True, True, 100, None, "Asset", now)
        self.assertIsNotNone(
            select_forecast(runtime, {"A": []}, {"A": asset}, {"A": now}, now)
        )
