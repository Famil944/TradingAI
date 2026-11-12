"""Read-only performance breakdown of recorded confirmation snapshots."""

import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from services.otc_confirmations import LABELS, VERSION
from services.otc_statistics import wilson


def _rows(path, query, since):
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(query, (since,))]


def read_records(root, since=0):
    root = Path(root)
    records = []
    for name, mode in (
        ("primary-ready.db", "Основной"),
        ("ml-preview.db", "XGBoost"),
        ("groq-preview.db", "Groq"),
    ):
        rows = _rows(
            root / name,
            """SELECT symbol,entry_at,expires_at-entry_at seconds,
            model_sha strategy,outcome,evidence FROM predictions WHERE entry_at>=?""",
            since,
        )
        for row in rows:
            payload = json.loads(row.pop("evidence"))
            row.update(mode=mode, confirmations=payload.get("confirmations"))
            records.append(row)
    path = root / "bot.db"
    if path.exists():
        rows = _rows(
            path,
            """SELECT p.symbol,p.entry_at,p.expires_at-p.entry_at seconds,
            p.strategy,COALESCE(a.outcome,CASE WHEN p.lifecycle='CANCELLED'
            THEN 'CANCELLED' ELSE 'PENDING' END) outcome,c.context
            FROM otc_conditional_plans p
            LEFT JOIN otc_plan_assessments a ON a.plan_id=p.id
            LEFT JOIN otc_plan_context c ON c.plan_id=p.id WHERE p.entry_at>=?""",
            since,
        )
        for row in rows:
            payload = json.loads(row.pop("context") or "{}")
            row.update(mode="Условный", confirmations=payload.get("confirmations"))
            records.append(row)
        rows = _rows(
            path,
            """SELECT symbol,signal_at entry_at,timeframe seconds,strategy,
            outcome,quality FROM otc_journal_signals WHERE signal_at>=?""",
            since,
        )
        for row in rows:
            payload = json.loads(row.pop("quality") or "{}")
            row.update(mode="Обычный", confirmations=payload.get("confirmations"))
            records.append(row)
    return records


def summarize(records):
    counts, combinations = defaultdict(Counter), defaultdict(Counter)
    missing = incomplete = 0
    for row in records:
        report = row.get("confirmations")
        if not report or report.get("version") != VERSION:
            missing += 1
            continue
        if report.get("available") != 5:
            incomplete += 1
            continue
        group = (row["mode"], row["strategy"], row["seconds"])
        counts[(*group, report["count"])][row["outcome"]] += 1
        combinations[(*group, tuple(report["matched"]))][row["outcome"]] += 1
    return dict(
        counts=counts,
        combinations=combinations,
        missing=missing,
        incomplete=incomplete,
        total=len(records),
    )


def _metrics(counts):
    wins, losses = counts["WIN"], counts["LOSS"]
    n = wins + losses
    interval = wilson(wins, losses)
    rate = f"{100 * wins / n:.1f}%" if n else "—"
    ci = (
        f" · 95% ДИ {100 * interval[0]:.0f}–{100 * interval[1]:.0f}%"
        if interval
        else ""
    )
    other = (
        sum(counts.values())
        - n
        - counts["DRAW"]
        - counts["UNKNOWN"]
        - counts["PENDING"]
    )
    return (
        f"+{wins} / −{losses} · {rate} (n={n}){ci}"
        f" · ничья {counts['DRAW']} · неизвестно {counts['UNKNOWN']}"
        f" · ожидают {counts['PENDING']} · отмена/пропуск {other}"
        + (" · мало данных" if n < 100 else "")
    )


def format_report(records):
    report = summarize(records)
    lines = [
        "📊 Подтверждения · виртуальные исходы OTCharts",
        f"Всего {report['total']} · без записи {report['missing']} · неполные {report['incomplete']}",
        "Неполные и старые записи не считаются нулём совпадений.",
    ]
    groups = sorted({key[:3] for key in report["counts"]})
    if not groups:
        lines.append(
            "Новые сигналы ещё не накоплены. История задним числом не размечается."
        )
    for mode, strategy, seconds in groups:
        group = mode, strategy, seconds
        lines.append(f"\n{mode} · M{seconds // 60} · {strategy}")
        for count in range(6):
            values = report["counts"].get((*group, count))
            if values:
                lines.append(f"{count}/5: {_metrics(values)}")
        lines.append("Точные комбинации:")
        for key, values in sorted(report["combinations"].items()):
            if key[:3] == group:
                names = "+".join(LABELS[k] for k in key[3]) or "Нет"
                lines.append(f"{names}: {_metrics(values)}")
    lines.append(
        "\nСовпадения связаны, это не прогнозные проценты. ДИ ориентировочный: сигналы могут зависеть друг от друга. Порог выдачи не изменён."
    )
    return "\n".join(lines)


if __name__ == "__main__":
    from config.settings import ROOT

    print(format_report(read_records(ROOT / "data")))
