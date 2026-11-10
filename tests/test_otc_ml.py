"""Causal features, purged splits, payout accounting and isolated XGBoost research."""

import copy
import importlib.util
import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from services.otc_ml.data import HORIZONS, Example, build_dataset
from services.otc_ml.features import FEATURE_NAMES, indicators
from services.otc_ml.model import ResearchModel, fit
from services.otc_ml.quality import block_intervals, expected_return, gate, policies
from services.otc_ml.research import run_research, train_horizon, watch
from services.otc_ml.validation import ResearchConfig, partitions
from services.otc_strategy import Candle

HAS_ML = all(importlib.util.find_spec(name) for name in ("xgboost", "sklearn", "numpy"))


def rows(n=1080):
    result = []
    for i in range(n):
        at = 1800000000 + i * 60
        opening = 100 + i * 0.001 + math.sin(i * 0.3) * 0.03
        close = 100 + (i + 1) * 0.001 + math.sin((i + 1) * 0.3) * 0.03
        result.append(
            dict(
                symbol="FIXTURE_otc",
                time=at,
                open=opening,
                high=max(opening, close) + 0.002,
                low=min(opening, close) - 0.002,
                close=close,
                source="otcharts",
                venue="otc",
                is_otc=True,
                verified=True,
                verification_evidence="SYNTHETIC fixture",
                available_at=at + 60,
                payout=95,
                payout_at=at,
                open_confirmed=True,
                platform_at=at,
                platform_open=opening,
                platform_evidence="SYNTHETIC fixture platform",
            )
        )
    return result


def examples(n=240, horizon=60, symbol="FIXTURE_otc"):
    return [
        Example(
            symbol,
            1800000000 + i * horizon,
            1800000000 + (i + 1) * horizon + 60,
            tuple(
                math.sin(i * 0.2 + j) + i * 0.0001 for j in range(len(FEATURE_NAMES))
            ),
            i % 3,
            95,
            "UP" if i % 5 == 0 else None,
            100,
            100 + (i % 3 - 1) * 0.01,
        )
        for i in range(n)
    ]


def small_config():
    return ResearchConfig(
        train_times=40,
        calibration_times=20,
        validation_times=20,
        test_times=30,
        min_folds=2,
        min_trades=5,
        min_days=2,
        min_class_count=2,
        bootstrap_replicates=40,
        trees=6,
    )


class FeatureDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = rows()
        cls.datasets, cls.counts = build_dataset(cls.source)

    def test_all_indicators_and_expirations_exist(self):
        self.assertEqual(set(self.datasets), set(HORIZONS))
        for data in self.datasets.values():
            self.assertTrue(data)
            self.assertEqual(len(data[0].features), len(FEATURE_NAMES))
            self.assertTrue(all(math.isfinite(v) for e in data for v in e.features))
            self.assertTrue(all(e.label_available_at > e.at for e in data))
        self.assertTrue(any("macd" in f for f in FEATURE_NAMES))
        self.assertIn("adx14", FEATURE_NAMES)
        self.assertIn("bb_width", FEATURE_NAMES)

    def test_future_ohlc_poison_does_not_change_earlier_features(self):
        source = copy.deepcopy(self.source)
        cutoff = source[1030]["time"]
        for row in source[1030:]:
            row["high"] = 1e6
            row["close"] = 1e5
        poisoned, _ = build_dataset(source)
        for horizon in HORIZONS:
            before = {
                e.at: e.features for e in self.datasets[horizon] if e.at <= cutoff
            }
            after = {e.at: e.features for e in poisoned[horizon] if e.at <= cutoff}
            self.assertEqual(before, after)

    def test_unknown_future_or_low_payout_produces_no_training_labels(self):
        for value in (None, 89):
            source = copy.deepcopy(self.source)
            for row in source:
                row["payout"] = value
            data, _ = build_dataset(source)
            self.assertFalse(any(data.values()))
        source = copy.deepcopy(self.source)
        for row in source:
            row["payout_at"] = row["time"] + 1
        self.assertFalse(any(build_dataset(source)[0].values()))

    def test_unverified_or_mismatched_platform_is_not_training_evidence(self):
        source = copy.deepcopy(self.source)
        for row in source:
            row["platform_open"] += 1
        self.assertFalse(any(build_dataset(source)[0].values()))
        source = copy.deepcopy(self.source)
        for row in source:
            row.pop("verification_evidence")
        self.assertFalse(any(build_dataset(source)[0].values()))

    def test_delayed_candle_availability_blocks_features(self):
        source = copy.deepcopy(self.source)
        cutoff = source[950]["time"] + 60
        source[950]["available_at"] += 100000
        data, counts = build_dataset(source)
        self.assertTrue(counts["features_not_yet_available"])
        self.assertFalse(any(e.at >= cutoff for data in data.values() for e in data))

    def test_duplicate_or_bad_ohlc_rejected(self):
        for change in ("duplicate", "ohlc"):
            source = copy.deepcopy(self.source[:5])
            if change == "duplicate":
                source[2]["time"] = source[1]["time"]
            else:
                source[2]["high"] = 0
            with self.assertRaises(ValueError):
                build_dataset(source)

    def test_known_trend_indicator_values(self):
        bars = [
            Candle(i * 60, 100 + i, 101 + i, 99.5 + i, 100.5 + i) for i in range(60)
        ]
        values = dict(zip(FEATURE_NAMES, indicators(bars)))
        close = 159.5
        self.assertAlmostEqual(values["ema20_gap"], 9.5 / close)
        self.assertAlmostEqual(values["ema50_gap"], 24.5 / close)
        self.assertAlmostEqual(values["ema_spread"], 15 / close)
        self.assertAlmostEqual(values["rsi14"], 1)
        self.assertAlmostEqual(values["adx14"], 1)
        self.assertAlmostEqual(values["atr14"], 1.5 / close)
        self.assertAlmostEqual(values["bb_width"], 4 * math.sqrt(33.25) / 150)
        macd = 12.5 * (1 - (1 - 2 / 27) ** 59) - 5.5 * (1 - (1 - 2 / 13) ** 59)
        self.assertAlmostEqual(values["macd"], macd / close)

    def test_flat_indicators_finite_and_gaps_rejected(self):
        bars = [Candle(i * 60, 1, 1, 1, 1) for i in range(60)]
        self.assertTrue(all(math.isfinite(v) for v in indicators(bars)))
        self.assertIsNone(indicators(bars[:30] + bars[31:]))
        self.assertIsNone(indicators(bars[:20]))


class SplitPolicyTests(unittest.TestCase):
    def test_splits_purge_label_availability_and_keep_symbol_time_cohorts(self):
        source = sorted(
            examples() + examples(symbol="SECOND_otc"), key=lambda e: (e.at, e.symbol)
        )
        folds, final = partitions(source, small_config())
        self.assertTrue(folds)
        for fold in folds + [final]:
            boundaries = fold["boundaries"]
            for name, next_at in (
                ("train", boundaries["calibration_from"]),
                ("calibration", boundaries["validation_from"]),
                ("validation", boundaries["test_from"]),
            ):
                self.assertTrue(
                    all(
                        e.at < next_at and e.label_available_at < next_at
                        for e in fold[name]
                    )
                )
            sets = [
                {e.at for e in fold[name]}
                for name in ("train", "calibration", "validation", "test")
            ]
            self.assertEqual(len(set.union(*sets)), sum(map(len, sets)))
            self.assertLessEqual(max(e.at for e in fold["test"]), boundaries["test_to"])
        self.assertTrue(
            all(
                e.at < final["boundaries"]["test_from"]
                for f in folds
                for e in f["test"]
            )
        )

    def test_ev_accounts_for_draw_refund_and_payout_floor(self):
        self.assertAlmostEqual(expected_return([0.3, 0.1, 0.6], "UP", 90), 0.24)
        self.assertAlmostEqual(expected_return([0.3, 0.1, 0.6], "DOWN", 90), -0.33)
        self.assertIsNone(expected_return([0.3, 0.1, 0.6], "UP", None))
        self.assertIsNone(expected_return([0.3, 0.1, 0.6], "UP", 89))
        with self.assertRaises(ValueError):
            expected_return([1, 1, 1], "UP", 90)

    def test_same_opportunities_and_cooldown_for_both_policies(self):
        source = [
            replace(e, baseline="UP", label=1 if i == 0 else e.label)
            for i, e in enumerate(examples(35))
        ]
        records = policies(source, [[0.1, 0.1, 0.8]] * len(source), small_config())
        self.assertEqual(
            [r["ml_trade"] for r in records], [r["baseline_trade"] for r in records]
        )
        self.assertTrue(all(r["ml_return"] == r["baseline_return"] for r in records))
        self.assertTrue(
            any(
                r["ml_trade"] and r["label"] == 1 and r["ml_return"] == 0
                for r in records
            )
        )

    def test_no_data_report_does_not_import_or_activate_models(self):
        with tempfile.TemporaryDirectory() as d:
            report = run_research(Path(d) / "absent.jsonl", Path(d) / "out")
            self.assertEqual(report["status"], "awaiting_verified_data")
            self.assertFalse(report["live_enabled"])
            self.assertFalse(report["probability_display_enabled"])
            self.assertTrue(
                all(r["model_path"] is None for r in report["horizons"].values())
            )
            self.assertTrue((Path(d) / "out" / "QUALITY_REPORT.md").exists())

    def test_watcher_survives_partially_written_history(self):
        with tempfile.TemporaryDirectory() as d:
            with (
                patch(
                    "services.otc_ml.research.run_research",
                    side_effect=ValueError("partial line"),
                ) as run,
                patch(
                    "services.otc_ml.research.time.sleep",
                    side_effect=[None, KeyboardInterrupt],
                ),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    watch(Path(d) / "input", Path(d) / "out", interval=10)
            self.assertEqual(run.call_count, 2)
            status = json.loads((Path(d) / "out" / "watch_status.json").read_text())
            self.assertEqual(status["status"], "waiting_for_valid_history")

    def test_recorded_future_labels_cannot_be_reported_as_historical_evidence(self):
        datasets = {h: examples(1, horizon=h) for h in HORIZONS}
        with tempfile.TemporaryDirectory() as d:
            with (
                patch(
                    "services.otc_ml.research.build_dataset",
                    return_value=(datasets, {"input_rows": 1}),
                ),
                patch("services.otc_ml.research.time.time", return_value=1800000000),
            ):
                with self.assertRaises(ValueError):
                    run_research(
                        Path(d) / "input", Path(d) / "out", data_kind="recorded"
                    )
            self.assertFalse((Path(d) / "out" / "report.json").exists())

    def test_protocol_validation(self):
        with self.assertRaises(ValueError):
            replace(small_config(), holdout_fraction=0.8).validate()


@unittest.skipUnless(HAS_ML, "Optional ML dependencies are not installed")
class ModelIntegrationTests(unittest.TestCase):
    def test_xgboost_calibration_portable_roundtrip_and_future_holdout_isolation(self):
        import numpy as np

        config = small_config()
        source = examples()
        _, split = partitions(source, config)
        model = fit(split["train"], split["calibration"], config)
        before = model.predict(split["test"])
        self.assertTrue(np.allclose(before.sum(axis=1), 1))
        # Mutating test labels cannot affect the already fitted booster/calibrator.
        changed = [replace(e, label=(e.label + 1) % 3) for e in split["test"]]
        self.assertTrue(np.array_equal(before, model.predict(changed)))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "model"
            model.save(path)
            loaded = ResearchModel.load(path)
            self.assertTrue(np.allclose(before, loaded.predict(split["test"])))
            self.assertTrue((path / "model.ubj").exists())
            self.assertFalse(any(path.glob("*.pkl")))

    def test_all_three_research_models_train_but_synthetic_never_qualifies(self):
        with tempfile.TemporaryDirectory() as d:
            for horizon in HORIZONS:
                result = train_horizon(
                    examples(horizon=horizon),
                    horizon,
                    small_config(),
                    "synthetic",
                    Path(d) / str(horizon),
                )
                self.assertEqual(result["status"], "research_only")
                self.assertIsNotNone(result["model_path"])
                self.assertFalse(result["eligible_for_review"])
                self.assertIn("synthetic_data_cannot_confirm_edge", result["reasons"])
                self.assertFalse(result["probability_display_enabled"])
                self.assertTrue(result["folds"])

    def test_retraining_requires_new_verified_examples_and_rejects_rewritten_history(
        self,
    ):
        datasets = {h: examples(horizon=h) for h in HORIZONS}
        with tempfile.TemporaryDirectory() as d:
            output = Path(d) / "out"
            with patch(
                "services.otc_ml.research.build_dataset",
                return_value=(datasets, {"input_rows": 240}),
            ):
                first = run_research(
                    Path(d) / "input",
                    output,
                    config=small_config(),
                    data_kind="synthetic",
                )
                second = run_research(
                    Path(d) / "input",
                    output,
                    config=small_config(),
                    data_kind="synthetic",
                    automatic=True,
                    min_new_verified=10,
                )
            self.assertTrue(
                all(
                    r["status"] == "waiting_for_new_verified_labels"
                    for r in second["horizons"].values()
                )
            )
            grown = {h: examples(260, horizon=h) for h in HORIZONS}
            with patch(
                "services.otc_ml.research.build_dataset",
                return_value=(grown, {"input_rows": 260}),
            ):
                third = run_research(
                    Path(d) / "input",
                    output,
                    config=small_config(),
                    data_kind="synthetic",
                    automatic=True,
                    min_new_verified=10,
                )
            self.assertNotEqual(
                first["horizons"]["60"]["model_path"],
                third["horizons"]["60"]["model_path"],
            )
            grown[60][0] = replace(grown[60][0], payout=90)
            with patch(
                "services.otc_ml.research.build_dataset",
                return_value=(grown, {"input_rows": 260}),
            ):
                fourth = run_research(
                    Path(d) / "input",
                    output,
                    config=small_config(),
                    data_kind="synthetic",
                    automatic=True,
                    min_new_verified=10,
                )
            self.assertEqual(
                fourth["horizons"]["60"]["status"],
                "history_changed_manual_revalidation_required",
            )
            self.assertFalse(fourth["live_enabled"])

    def test_day_block_interval_preserves_shared_day_clustering(self):
        records = []
        for day in range(25):
            for _ in range(4):
                records.append(
                    dict(
                        at=1800000000 + day * 86400,
                        ml_return=0.9,
                        baseline_return=0,
                        ml_trade=True,
                    )
                )
        result = block_intervals(records, small_config())
        self.assertEqual(result["days"], 25)
        self.assertAlmostEqual(result["ml_return_per_trade_95"][0], 0.9)
        self.assertAlmostEqual(result["paired_improvement_per_opportunity_95"][0], 0.9)

    def test_selected_signal_miscalibration_blocks_even_good_global_ece(self):
        model = type("Model", (), {"calibration_counts": [50, 50, 50]})()
        evaluation = {
            "ml": {"trades": 200},
            "intervals": {
                "days": 30,
                "ml_return_per_trade_95": [0.1, 0.3],
                "paired_improvement_per_opportunity_95": [0.01, 0.03],
            },
            "calibration": {"ece": 0.01, "brier": 0.1},
            "selected_calibration": {"ece": 0.3},
            "climatology": {"brier": 0.2},
        }
        result = gate(evaluation, model, small_config())
        self.assertFalse(result["passed"])
        self.assertEqual(
            result["reasons"], ["selected_signal_calibration_not_confirmed"]
        )

    def test_gate_never_accepts_uncertain_or_uncalibrated_results(self):
        model = type("Model", (), {"calibration_counts": [0, 1, 0]})()
        evaluation = {
            "ml": {"trades": 0},
            "intervals": {
                "days": 1,
                "ml_return_per_trade_95": None,
                "paired_improvement_per_opportunity_95": None,
            },
            "calibration": {"ece": None, "brier": None},
            "climatology": {"brier": None},
        }
        result = gate(evaluation, model, small_config())
        self.assertFalse(result["passed"])
        self.assertIn("positive_return_not_confirmed", result["reasons"])


if __name__ == "__main__":
    unittest.main()
