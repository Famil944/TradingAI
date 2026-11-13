"""Lazy checked model integration: uncertified source studies can only run in shadow."""

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from services.otc_strategy import aggregate

from .features import FEATURE_NAMES, FEATURE_VERSION, indicators
from .quality import expected_return


def approved(report, result):
    config = report.get("configuration", {})
    floors = {"min_folds": 3, "min_days": 20, "min_trades": 100, "min_class_count": 20}
    if (
        report.get("data_kind") != "recorded"
        or result.get("eligible_for_review") is not True
        or result.get("reasons")
    ):
        return False
    if any(config.get(key, 0) < minimum for key, minimum in floors.items()):
        return False
    recent = result.get("folds", [])[-config["min_folds"] :]
    final = result.get("final_holdout", {})
    return (
        len(recent) == config["min_folds"]
        and all(
            f.get("validation_gate", {}).get("passed")
            and f.get("test_gate", {}).get("passed")
            for f in recent
        )
        and final.get("validation_gate", {}).get("passed") is True
        and final.get("test_gate", {}).get("passed") is True
    )


class RuntimeML:
    def __init__(self, verified_root, study_root):
        self.roots = (Path(verified_root).resolve(), Path(study_root).resolve())
        self.models = {}
        self.reports = {}

    def read_report(self, root):
        path = root / "report.json"
        stamp = path.stat().st_mtime_ns
        cached = self.reports.get(str(path))
        if cached is None or cached[0] != stamp:
            cached = (stamp, json.loads(path.read_text(encoding="utf-8")))
            self.reports[str(path)] = cached
        return cached[1]

    def status(self):
        path = self.roots[1] / "report.json"
        if not path.exists():
            return "XGBoost: сбор истории / обучение; влияние на сигналы выключено."
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            trained = [
                f"M{int(h) // 60}"
                for h, r in report.get("horizons", {}).items()
                if r.get("model_path")
            ]
            return (
                "XGBoost: теневые модели "
                + (", ".join(trained) or "ещё не обучены")
                + ". Проверенные модели допускаются отдельно; прогнозные проценты скрыты."
            )
        except (OSError, ValueError):
            return "XGBoost: отчёт временно недоступен."

    def requires_filter(self, row):
        try:
            report = self.read_report(self.roots[0])
            h = str(int(row["expires_at"] - row["entry_at"]))
            result = report.get("horizons", {}).get(h, {})
            return approved(report, result)
        except (OSError, ValueError, TypeError):
            return False

    def candidates(self, seconds, decision_lead):
        for index, root in enumerate(self.roots):
            path = root / "report.json"
            if not path.exists():
                continue
            report = self.read_report(root)
            result = report.get("horizons", {}).get(str(seconds), {})
            if report.get("feature_version") != FEATURE_VERSION or report.get(
                "feature_names"
            ) != list(FEATURE_NAMES):
                continue
            if report.get("decision_lead_seconds", 0) != decision_lead:
                continue
            created = report.get("created_at", 0)
            if isinstance(created, str):
                created = datetime.fromisoformat(created).timestamp()
            if not 0 <= time.time() - created <= 7 * 86400:
                continue
            certified = index == 0 and approved(report, result)
            location = result.get("model_path")
            if not location:
                continue
            model_path = Path(location).resolve()
            if not model_path.is_relative_to(root):
                continue
            hashes = tuple(
                hashlib.sha256((model_path / name).read_bytes()).hexdigest()
                for name in ("model.ubj", "calibration.json")
            )
            if hashes != (result.get("model_sha256"), result.get("calibration_sha256")):
                continue
            yield report, certified, model_path, hashes

    def preview_ready(self):
        for seconds in (60, 120, 180, 300, 900):
            try:
                if next(self.candidates(seconds, 20), None) is not None:
                    return True
            except (OSError, ValueError, TypeError):
                continue
        return False

    def inspect(self, row, bars, payout):
        seconds = int(row["expires_at"] - row["entry_at"])
        for report, certified, model_path, hashes in self.candidates(
            seconds, row.get("decision_lead_seconds", 0)
        ):
            cutoff = row.get("feature_asof", row["entry_at"])
            past = [b for b in bars if b.time + 60 <= cutoff][-1200:]
            features = indicators(
                aggregate(past, report.get("feature_period_seconds") or seconds)
            )
            if features is None:
                return {"status": "insufficient_live_features", "live": False}
            key = (str(model_path), hashes)
            if key not in self.models:
                from .model import ResearchModel

                self.models[key] = ResearchModel.load(model_path)
                if len(self.models) > 12:
                    self.models.pop(next(iter(self.models)))
            probabilities = (
                self.models[key]
                .predict([SimpleNamespace(features=features)])[0]
                .tolist()
            )
            config = report.get("configuration", {})
            p = probabilities[2 if row["direction"] == "UP" else 0]
            ev = expected_return(probabilities, row["direction"], payout)
            passed = (
                p >= config.get("probability_floor", 0.60)
                and ev is not None
                and ev >= config.get("min_expected_return", 0.05)
            )
            return dict(
                status="validated_filter" if certified else "shadow",
                live=certified,
                accept=passed,
                probabilities=probabilities,
                expected_return=ev,
                model_sha256=hashes[0],
                data_kind=report["data_kind"],
                probabilities_display_allowed=certified,
            )
        return {"status": "awaiting_qualified_model", "live": False}
