"""Descriptive evaluation of all prospectively logged ready candidates.

Outcomes are OTCharts historical virtual opens, NOT Pocket Option settlements.
Rejected opportunities are retained; filters must be validated on future days.
"""
import json
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path


def summarize(rows):
    counts = Counter(x[1] for x in rows)
    wins, losses = counts["WIN"], counts["LOSS"]
    returns = [
        (r["payout"] / 100 if outcome == "WIN" else -1 if outcome == "LOSS" else 0)
        for r, outcome in rows
        if outcome in ("WIN", "LOSS", "DRAW")
        and isinstance(r.get("payout"), (int, float))
        and 0 <= r["payout"] <= 100
    ]
    return dict(
        opportunities=len(rows),
        wins=wins,
        losses=losses,
        draws=counts["DRAW"],
        unknown=counts["UNKNOWN"],
        win_rate=round(wins / (wins + losses), 4) if wins + losses else None,
        mean_units=round(sum(returns) / len(returns), 4) if returns else None,
        priced=len(returns),
    )


def report(data_dir, since=0, until=None, limit=10000):
    now = time.time() if until is None else until
    folder = Path(data_dir)
    journal = folder / "ready-candidates.db"
    archive = folder / "market-history.db"
    output = dict(
        source="OTCharts virtual historical opens; no broker execution proof",
        status="no_data",
        candidates=0,
        truncated=False,
        by_reason={},
        by_regime={},
        by_timeframe={},
        by_policy={},
        rejected_filters={},
    )
    if not journal.exists():
        return output
    with closing(sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        records = [
            json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM candidates WHERE version=? AND observed_at>=? "
                "AND observed_at<=? ORDER BY observed_at DESC LIMIT ?",
                ("primary-ready-v7-confirmation-gate", since, now, limit + 1),
            )
        ]
    if len(records) > limit:
        output["truncated"] = True
        records = records[:limit]
    output["candidates"] = len(records)
    if not records:
        return output
    output["status"] = "exploratory"
    times = {(r["symbol"], t)
             for r in records
             for t in (r["entry_at"], r["expires_at"])}
    canonical = {}
    if archive.exists():
        with closing(sqlite3.connect(archive.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            start = min(r["entry_at"] for r in records)
            end = max(r["expires_at"] for r in records)
            for symbol, stamp, opening in db.execute(
                "SELECT symbol,time,open FROM bars WHERE time BETWEEN ? AND ? "
                "AND source='otcharts_history' AND revisions=0 AND first_observed<=? "
                "AND last_observed<=?",
                (start, end, now, now),
            ):
                if (symbol, stamp) in times:
                    canonical[(symbol, stamp)] = opening
    groups = defaultdict(list)
    for r in records:
        start = canonical.get((r["symbol"], r["entry_at"]))
        end = canonical.get((r["symbol"], r["expires_at"]))
        direction = r.get("direction")
        if start is None or end is None or direction not in ("UP", "DOWN"):
            outcome = "UNKNOWN"
        elif end == start:
            outcome = "DRAW"
        else:
            outcome = (
                "WIN" if (end > start) == (direction == "UP") else "LOSS"
            )
        item = (r, outcome)
        groups[("reason", r.get("reason", "unknown"))].append(item)
        groups[("regime", r.get("regime", "unknown"))].append(item)
        groups[("timeframe", "M" + str(int((r["expires_at"] - r["entry_at"]) / 60)))].append(item)
        groups[("policy", "eligible" if r.get("reason") == "eligible" else "rejected")].append(item)
        for name, gate in r.get("gates", {}).items():
            if gate is False:
                groups[("rejected_filters", name)].append(item)
    for key, label in (("reason", "by_reason"), ("regime", "by_regime"),
                       ("timeframe", "by_timeframe"), ("policy", "by_policy"),
                       ("rejected_filters", "rejected_filters")):
        output[label] = {
            name: summarize(items) for (group, name), items in sorted(groups.items())
            if group == key
        }
    return output


def format_report(data):
    if data["status"] == "no_data":
        return "Пока нет кандидатов для нового анализа фильтров. Запусти live-сеанс."
    lines = [
        "Фильтры OTC · экспериментальные результаты",
        f"Проверено кандидатов: {data['candidates']}"
        + (" (последние; лимит отчёта)" if data["truncated"] else ""),
        "Исходы только виртуальные по OTCharts, не Pocket Option.",
    ]
    for title, field in (
        ("Допуск / пропуск", "by_policy"), ("Рыночные режимы", "by_regime"),
        ("Экспирации", "by_timeframe"), ("Причины отказа", "by_reason"),
        ("Не пройденные проверки", "rejected_filters"),
    ):
        lines.append("\n" + title)
        for name, m in data[field].items():
            rate = f"{m['win_rate']:.1%}" if m["win_rate"] is not None else "—"
            lines.append(
                f"{name}: {m['opportunities']} · WIN {m['wins']} / LOSS {m['losses']} "
                f"· UNKNOWN {m['unknown']} · {rate}"
            )
    lines.append("\nСравнение не доказывает пользу фильтра: нужны новые дни и одинаковые возможности входа.")
    return "\n".join(lines)
