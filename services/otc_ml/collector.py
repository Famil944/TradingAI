"""No-network background training from the actual local OTCharts archive."""

import argparse
import json
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from config.settings import ROOT
from services.otc_market_archive import MarketArchive

from .features import FEATURE_NAMES, FEATURE_VERSION
from .provider_data import build_provider_dataset
from .research import atomic_json, train_horizon
from .validation import ResearchConfig


def run_once(archive, output, minimum_new=1000, *, preview=False):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "provider-history.jsonl"
    state_path = output / "collection-state.json"
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    # Avoid a costly full SQLite export every cycle when history is unchanged.
    count = archive.clean_bars_count()
    if count < 1000 or (previous and count - previous.get("rows", 0) < minimum_new):
        return {"status": "collecting", "rows": count}
    count = archive.export(path)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    datasets, counts = build_provider_dataset(rows, preview=preview)
    experiment = uuid.uuid4().hex
    report = dict(
        schema_version=1,
        data_kind="provider_retrospective",
        created_at=time.time(),
        feature_version=FEATURE_VERSION,
        feature_names=list(FEATURE_NAMES),
        decision_lead_seconds=20 if preview else 0,
        feature_period_seconds=60 if preview else None,
        status="research_only",
        live_enabled=False,
        probability_display_enabled=False,
        dataset_quality=counts,
        horizons={},
        configuration_by_horizon={},
    )
    for seconds, examples in datasets.items():
        n = len({e.at for e in examples})
        # Small diagnostic cohorts do NOT relax production quality gates (20 days etc.).
        config = ResearchConfig(
            train_times=max(80, n // 5),
            calibration_times=max(30, n // 10),
            validation_times=max(30, n // 10),
            test_times=max(30, n // 10),
        )
        report["configuration_by_horizon"][str(seconds)] = asdict(config)
        destination = output / "runs" / experiment / f"M{seconds // 60}"
        report["horizons"][str(seconds)] = train_horizon(
            examples, seconds, config, "provider_retrospective", destination
        )
        result = report["horizons"][str(seconds)]
        result["retrospective_examples"] = result.pop("verified_examples")
    atomic_json(output / "runs" / experiment / "report.json", report)
    atomic_json(output / "report.json", report)
    atomic_json(state_path, {"rows": count, "trained_at": time.time()})
    lines = [
        "# XGBoost на котировках OTCharts",
        "",
        "Ретроспективное исследование. Исторические выплаты неизвестны: сравнение при условной выплате 90%.",
        "Сверка источника подтверждена пользователем; независимые цены каждого исторического входа не восстановлены.",
        "Влияние на сигналы и показ прогнозных процентов отключены до строгой проверки.",
        "",
    ]
    for h, r in report["horizons"].items():
        lines += [
            f"## M{int(h) // 60}",
            f"Статус: {r['status']}; примеров: {r['retrospective_examples']}",
            "Причины недопуска: " + ", ".join(r.get("reasons", [])),
        ]
        final = r.get("final_holdout")
        if final:
            for role in ("ml", "baseline"):
                m = final["test"][role]
                lines.append(
                    f"{role}: WIN {m['wins']}, LOSS {m['losses']}, сигналов {m['trades']}, винрейт {m['win_rate']}, результат при условных 90% {m['net_units']:.2f}"
                )
        lines.append("")
    (output / "QUALITY_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    return report


def main():
    from core.single_instance import SingleInstance

    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    name = "ml_preview20" if args.preview else "ml_provider"
    lock = SingleInstance(port=47656 if args.preview else 47655)
    if not lock.acquire():
        return
    (
        ROOT
        / "logs"
        / ("ml-preview-collector.pid" if args.preview else "ml-collector.pid")
    ).write_text(str(os.getpid()), encoding="utf-8")
    archive = MarketArchive(ROOT / "data" / "market-history.db")
    try:
        while True:
            try:
                result = run_once(archive, ROOT / "data" / name, preview=args.preview)
                from database.db import Database

                from .forward import comparison

                atomic_json(
                    ROOT / "data" / name / "forward-comparison.json",
                    comparison(Database(), time.time()),
                )
                print("XGBoost collector:", result["status"], flush=True)
            except Exception as exc:
                print("XGBoost collector retry:", type(exc).__name__, flush=True)
            time.sleep(max(60, int(os.getenv("OTC_ML_COLLECT_SECONDS", "300"))))
    finally:
        lock.close()


if __name__ == "__main__":
    main()
