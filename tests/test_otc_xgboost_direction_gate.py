"""UP placeholder must not reject actual DOWN XGBoost forecasts."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.otc_ml.runtime import RuntimeML
from services.otc_ml.preview import select_forecast
from services.otc_market import Asset


class DirectionGateTests(unittest.TestCase):
    def inspect(self, probabilities, certified=True, floor=0.6):
        runtime = RuntimeML("/unused/verified", "/unused/research")
        report = dict(configuration=dict(
            probability_floor=floor, min_expected_return=0.05
        ), data_kind="recorded")
        # Avoid loading model files and heavy xgboost dependencies.
        class Fake:
            def predict(self, rows):
                return [SimpleNamespace(tolist=lambda: probabilities)]
        runtime.models[("model", ("hash", "calhash"))] = Fake()
        row = dict(entry_at=1800000060, expires_at=1800000120,
                   direction="UP", feature_asof=1800000042, decision_lead_seconds=20)
        with (
            patch.object(runtime, "candidates", return_value=iter([
                (report, certified, "/unused/model", ("hash", "calhash"))
            ])),
            patch("services.otc_ml.runtime.aggregate", return_value=[]),
            patch("services.otc_ml.runtime.indicators", return_value=(1,)),
        ):
            result = runtime.inspect(row, [], 90)
        return result

    def test_down_at_80_percent_passes_its_own_gate(self):
        result = self.inspect([0.8, 0, 0.2])
        self.assertEqual(result["selected_direction"], "DOWN")
        self.assertEqual(result["direction_probability"], 0.8)
        self.assertTrue(result["accept"])
        self.assertGreater(result["expected_return"], 0)

    def test_up_at_80_percent_passes_its_own_gate(self):
        result = self.inspect([0.2, 0, 0.8])
        self.assertEqual(result["selected_direction"], "UP")
        self.assertTrue(result["accept"])

    def test_research_not_restricted_by_certified_threshold(self):
        result = self.inspect([0.5, 0, 0.5], certified=False)
        self.assertTrue(result["accept"])
        self.assertFalse(result["threshold_passed"])
        self.assertFalse(result["threshold_applied"])

    def test_zero_implicit_probability_floor(self):
        result = self.inspect([0.5, 0.1, 0.4], certified=True, floor=None)
        self.assertEqual(result["selected_direction"], "DOWN")
        self.assertIsNone(result["probability_floor"])
        self.assertTrue(result["accept"])


class SelectionDirectionTests(unittest.TestCase):
    @patch("services.otc_ml.preview.build",
           return_value={"count": 1, "available": 5, "total": 5})
    def test_down_selection_with_real_direction_result(self, build):
        now = 1800000042
        asset = Asset("A", True, True, 90, None, "A", now)
        class Runtime:
            def inspect(self, *_):
                return dict(probabilities=[0.8, 0, 0.2], selected_direction="DOWN",
                            accept=True, model_sha256="fixture")
        selected = select_forecast(Runtime(), {"A": []}, {"A": asset}, {"A": now}, now)
        self.assertEqual(selected["direction"], "DOWN")
