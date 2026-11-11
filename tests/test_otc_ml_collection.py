import json
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.otc_market_archive import MarketArchive
from services.otc_ml.features import FEATURE_NAMES, FEATURE_VERSION
from services.otc_ml.provider_data import build_provider_dataset
from services.otc_ml.runtime import RuntimeML
from services.otc_strategy import Candle
from tests.test_otc_conditional import impulse_history


class MLCollectionTests(unittest.TestCase):
    def test_archive_deduplicates_excludes_future_and_records_revisions(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = MarketArchive(Path(folder) / "history.db")
            b = Candle(1800000000, 1, 1.1, 0.9, 1)
            archive.bars("A", [b], b.time + 60)
            archive.bars("A", [b], b.time + 120)
            archive.bars("A", [Candle(b.time + 120, 1, 1.1, 0.9, 1)], b.time + 120)
            path = Path(folder) / "history.jsonl"
            self.assertEqual(archive.export(path), 1)
            row = json.loads(path.read_text())
            self.assertEqual(row["first_observed"], b.time + 60)
            self.assertNotIn("payout", row)
            self.assertNotIn("platform_evidence", row)
            archive.bars("A", [Candle(b.time, 1, 1.2, 0.9, 1.1)], b.time + 180)
            self.assertEqual(archive.export(path), 0)

    def test_retrospective_features_do_not_use_entry_or_future_candle(self):
        bars = impulse_history()
        # Repeat the last minute to provide complete endpoint labels.
        for i in range(1, 20):
            bars.append(Candle(bars[900].time + i * 60, 102, 103, 101, 102))
        rows = [
            dict(asdict(b), symbol="A", source="otcharts", venue="otc", is_otc=True)
            for b in bars
        ]
        before, counts = build_provider_dataset(rows)
        self.assertTrue(before[60])
        changed = [dict(r) for r in rows]
        changed[900].update(open=200, high=220, low=190, close=210)
        after, _ = build_provider_dataset(changed)
        self.assertEqual(before[60][0].features, after[60][0].features)
        self.assertFalse(counts["actual_historical_payouts_available"])
        self.assertFalse(counts["independent_endpoint_evidence"])
        with self.assertRaises(ValueError):
            build_provider_dataset(rows + [rows[0]])

    def test_provider_model_cannot_enable_live_filter_even_with_claimed_approval(self):
        import hashlib

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            study = root / "study"
            study.mkdir()
            artifacts = study / "model"
            artifacts.mkdir()
            for name in ("model.ubj", "calibration.json"):
                (artifacts / name).write_text("fixture")
            report = dict(
                data_kind="provider_retrospective",
                created_at=time.time(),
                feature_version=FEATURE_VERSION,
                feature_names=list(FEATURE_NAMES),
                horizons={
                    "60": dict(
                        eligible_for_review=True,
                        reasons=[],
                        model_path=str(artifacts),
                        model_sha256=hashlib.sha256(b"fixture").hexdigest(),
                        calibration_sha256=hashlib.sha256(b"fixture").hexdigest(),
                    )
                },
            )
            (study / "report.json").write_text(json.dumps(report))
            runtime = RuntimeML(root / "verified", study)
            fake = SimpleNamespace(
                predict=lambda examples: SimpleNamespace(__getitem__=None)
            )

            class Prediction:
                def __getitem__(self, index):
                    return SimpleNamespace(tolist=lambda: [0.1, 0.0, 0.9])

            fake.predict = lambda examples: Prediction()
            bars = impulse_history()
            row = dict(
                entry_at=bars[-1].time + 60,
                expires_at=bars[-1].time + 120,
                direction="UP",
            )
            with patch("services.otc_ml.model.ResearchModel.load", return_value=fake):
                result = runtime.inspect(row, bars, 92)
            self.assertEqual(result["status"], "shadow")
            self.assertFalse(result["live"])
            self.assertFalse(result["probabilities_display_allowed"])
            self.assertFalse(runtime.requires_filter(row))

    def test_reduced_diagnostic_protocol_cannot_enable_production(self):
        from services.otc_ml.runtime import approved

        report = {
            "data_kind": "recorded",
            "configuration": {
                "min_folds": 1,
                "min_days": 1,
                "min_trades": 1,
                "min_class_count": 1,
            },
        }
        result = {
            "eligible_for_review": True,
            "reasons": [],
            "folds": [],
            "final_holdout": {},
        }
        self.assertFalse(approved(report, result))
