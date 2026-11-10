"""Offline experiments and automatic research-only retraining on verified files."""

import hashlib
import json
import os
import platform
import time
import uuid
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from .data import HORIZONS, build_dataset, dataset_digest
from .features import FEATURE_NAMES, FEATURE_VERSION
from .model import fit
from .quality import evaluate, gate
from .validation import ResearchConfig, partitions


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def fold_report(split, config):
    if any(not split[name] for name in ("train", "calibration", "validation", "test")):
        raise ValueError("Purging left an empty chronological partition")
    model = fit(split["train"], split["calibration"], config)
    validation, _ = evaluate(split["validation"], model, split["train"], config)
    prior_gate = gate(validation, model, config)
    # Test labels never select parameters, thresholds or calibrator coefficients.
    test, records = evaluate(split["test"], model, split["train"], config)
    report = dict(
        boundaries=split["boundaries"],
        sizes={
            k: len(split[k]) for k in ("train", "calibration", "validation", "test")
        },
        calibration_class_counts=model.calibration_counts,
        validation=validation,
        validation_gate=prior_gate,
        test=test,
        test_gate=gate(test, model, config),
        would_enable_during_test=prior_gate["passed"],
    )
    if not prior_gate["passed"]:
        report["gated_test_trades"] = 0
    else:
        report["gated_test_trades"] = test["ml"]["trades"]
    return report, model, records


def train_horizon(examples, horizon, config, kind, model_path):
    folds, final = partitions(examples, config)
    result = dict(
        horizon_seconds=horizon,
        verified_examples=len(examples),
        status="insufficient_data",
        eligible_for_review=False,
        live_enabled=False,
        probability_display_enabled=False,
        model_path=None,
        folds=[],
    )
    if final is None:
        result["reasons"] = [
            "insufficient_chronological_history_after_holdout_and_purging"
        ]
        return result
    for number, split in enumerate(folds):
        try:
            fold, _, _ = fold_report(split, config)
            fold["fold"] = number
            result["folds"].append(fold)
        except ValueError as exc:
            result["folds"].append(
                dict(fold=number, status="not_fitted", reason=str(exc))
            )
    try:
        final_report, model, records = fold_report(final, config)
    except ValueError as exc:
        result["status"] = "insufficient_classes_or_partitions"
        result["reasons"] = [str(exc)]
        return result
    model.save(model_path)
    result["model_path"] = str(model_path)
    result["final_holdout"] = final_report
    result["final_holdout_records"] = records
    reasons = []
    if kind != "recorded":
        reasons.append("synthetic_data_cannot_confirm_edge")
    # Predeclared recent consecutive windows; do not cherry-pick successful folds.
    recent = result["folds"][-config.min_folds :]
    if len(recent) < config.min_folds or not all(
        f.get("validation_gate", {}).get("passed")
        and f.get("test_gate", {}).get("passed")
        for f in recent
    ):
        reasons.append("recent_consecutive_walk_forward_folds_not_confirmed")
    if not final_report["validation_gate"]["passed"]:
        reasons.append("final_pre_holdout_validation_failed")
    if not final_report["test_gate"]["passed"]:
        reasons.extend(final_report["test_gate"]["reasons"])
    result["status"] = "research_only"
    result["reasons"] = reasons
    result["eligible_for_review"] = not reasons
    result["model_sha256"] = hashlib.sha256(
        (Path(model_path) / "model.ubj").read_bytes()
    ).hexdigest()
    result["calibration_sha256"] = hashlib.sha256(
        (Path(model_path) / "calibration.json").read_bytes()
    ).hexdigest()
    return result


def run_research(
    input_path,
    output_path,
    *,
    config=None,
    data_kind="recorded",
    automatic=False,
    min_new_verified=1000,
):
    config = config or ResearchConfig()
    config.validate()
    if data_kind not in ("recorded", "synthetic") or min_new_verified < 1:
        raise ValueError("Invalid input kind or retraining threshold")
    input_path, output_path = Path(input_path), Path(output_path)
    if input_path.resolve() in {
        (output_path / name).resolve()
        for name in ("report.json", "retraining_state.json", "QUALITY_REPORT.md")
    }:
        raise ValueError("Report must not overwrite history input")
    raw = input_path.read_bytes() if input_path.exists() else b""
    try:
        rows = [
            json.loads(line)
            for line in raw.decode("utf-8-sig").splitlines()
            if line.strip()
        ]
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError(
            "History must be UTF-8 JSONL; credentials are not history data"
        ) from None
    datasets, counts = build_dataset(rows)
    if data_kind == "recorded" and any(
        e.label_available_at > time.time() for data in datasets.values() for e in data
    ):
        raise ValueError(
            "Recorded history contains labels not yet available in real time"
        )
    experiment = uuid.uuid4().hex
    created = datetime.now(timezone.utc).isoformat()
    implementation = hashlib.sha256()
    for source_path in sorted(Path(__file__).parent.glob("*.py")) + [
        Path(__file__).parent.parent / "otc_strategy.py"
    ]:
        implementation.update(source_path.name.encode())
        implementation.update(source_path.read_bytes())
    implementation_sha256 = implementation.hexdigest()
    report = dict(
        schema_version=1,
        experiment=experiment,
        created_at=created,
        data_kind=data_kind,
        input_sha256=hashlib.sha256(raw).hexdigest(),
        feature_version=FEATURE_VERSION,
        implementation_sha256=implementation_sha256,
        python_version=platform.python_version(),
        feature_names=list(FEATURE_NAMES),
        configuration=config.to_dict(),
        configuration_sha256=hashlib.sha256(
            json.dumps(config.to_dict(), sort_keys=True).encode()
        ).hexdigest(),
        dataset_quality=counts,
        status="awaiting_verified_data"
        if not any(datasets.values())
        else "research_only",
        live_enabled=False,
        probability_display_enabled=False,
        interpretation="Hypothetical verified minute-open outcomes, not real broker entries or proven live win probabilities.",
        caution="Day-block bootstrap is an empirical dependence-aware interval, not a guarantee. No model is connected to Telegram signals.",
        horizons={},
    )
    state_path = output_path / "retraining_state.json"
    previous = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {}
    )
    # Changing criteria/data kind must force a fresh manually reviewed experiment.
    same_protocol = (
        previous.get("configuration_sha256") == report["configuration_sha256"]
        and previous.get("data_kind") == data_kind
        and previous.get("implementation_sha256") == implementation_sha256
    )
    state = dict(
        configuration_sha256=report["configuration_sha256"],
        implementation_sha256=implementation_sha256,
        data_kind=data_kind,
        horizons={},
    )
    for horizon in HORIZONS:
        examples = datasets[horizon]
        old = (
            previous.get("horizons", {}).get(str(horizon), {}) if same_protocol else {}
        )
        old_count = old.get("verified_count", 0)
        if automatic and old:
            if len(examples) < old_count or dataset_digest(
                examples[:old_count]
            ) != old.get("dataset_digest"):
                report["horizons"][str(horizon)] = dict(
                    status="history_changed_manual_revalidation_required",
                    live_enabled=False,
                    eligible_for_review=False,
                )
                state["horizons"][str(horizon)] = old
                continue
            if len(examples) - old_count < min_new_verified:
                report["horizons"][str(horizon)] = dict(
                    status="waiting_for_new_verified_labels",
                    new_verified=len(examples) - old_count,
                    required=min_new_verified,
                    previous_research_report=old.get("research_report"),
                    live_enabled=False,
                    eligible_for_review=False,
                )
                state["horizons"][str(horizon)] = old
                continue
        destination = output_path / "runs" / experiment / f"M{horizon // 60}"
        result = train_horizon(examples, horizon, config, data_kind, destination)
        report["horizons"][str(horizon)] = result
        if result["model_path"]:
            state["horizons"][str(horizon)] = dict(
                verified_count=len(examples),
                dataset_digest=dataset_digest(examples),
                research_report=str(output_path / "runs" / experiment / "report.json"),
                model_path=result["model_path"],
            )
    trained = any(r.get("model_path") for r in report["horizons"].values())
    if trained:
        report["versions"] = {
            name: version(name)
            for name in ("xgboost", "scikit-learn", "numpy", "scipy")
        }
    atomic_json(output_path / "runs" / experiment / "report.json", report)
    atomic_json(state_path, state)
    atomic_json(output_path / "report.json", report)
    write_markdown(output_path / "QUALITY_REPORT.md", report)
    return report


def write_markdown(path, report):
    lines = [
        "# XGBoost OTC — отчёт исследования",
        "",
        f"Статус: {report['status']}",
        f"Данные: {report['data_kind']}. Входных строк: {report['dataset_quality'].get('input_rows', 0)}.",
        "",
        "Модели не подключены к Telegram. Показ вероятности выигрыша выключен.",
        "Оцениваются гипотетические входы на точном открытии минуты, не реальные сделки.",
        "",
        "## Модели M1 / M5 / M15",
        "",
    ]
    for key, result in report["horizons"].items():
        lines.append(f"### M{int(key) // 60}")
        lines.append(
            f"Статус: {result['status']}; проверенных примеров: {result.get('verified_examples', 'см. предыдущий отчёт')}."
        )
        lines.append(
            f"Допуск только к дальнейшему рассмотрению: {result.get('eligible_for_review', False)}."
        )
        if result.get("reasons"):
            lines.append("Причины: " + ", ".join(result["reasons"]) + ".")
        final = result.get("final_holdout")
        if final:
            for name in ("ml", "baseline"):
                m = final["test"][name]
                lines.append(
                    f"{name}: сигналов {m['trades']}, результат {m['net_units']:.3f} ед., средний результат {m['return_per_trade']}, просадка {m['max_drawdown_units']:.3f} ед."
                )
            lines.append(
                f"Калибровка на независимом holdout: {final['test']['calibration']}."
            )
            lines.append(f"Интервалы по дневным блокам: {final['test']['intervals']}.")
        lines.append("")
    lines += [
        "## Ограничения",
        "",
        "Без реальной проверенной истории преимущество не подтверждено. Синтетические данные проверяют только работу кода.",
        "Выплата ≥90% и точные независимые цены обязательны для учебных меток; неизвестные исходы не становятся выигрышами.",
        "Полные настройки, границы периодов, хеши и результаты всех окон: report.json.",
    ]
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)


def watch(
    input_path,
    output_path,
    *,
    config=None,
    data_kind="recorded",
    min_new_verified=1000,
    interval=60,
):
    if interval < 10:
        raise ValueError("Watcher interval must be at least 10 seconds")
    last_digest = None
    while True:
        path = Path(input_path)
        raw = path.read_bytes() if path.exists() else b""
        digest = hashlib.sha256(raw).hexdigest()
        if digest != last_digest:
            try:
                result = run_research(
                    input_path,
                    output_path,
                    config=config,
                    data_kind=data_kind,
                    automatic=True,
                    min_new_verified=min_new_verified,
                )
                atomic_json(
                    Path(output_path) / "watch_status.json",
                    dict(status="watching", last_experiment=result["experiment"]),
                )
                last_digest = digest
            except (ValueError, OSError) as exc:
                # An append-in-progress is not a reason to stop automatic research.
                atomic_json(
                    Path(output_path) / "watch_status.json",
                    dict(
                        status="waiting_for_valid_history",
                        error_type=type(exc).__name__,
                    ),
                )
        time.sleep(interval)
