"""Matched M1 baseline vs strict research, using only existing canonical OHLC.

No auto-promotion. Unknown prices/payouts are not counted as wins/losses.
Broker-side prices and actual execution are NOT available.
"""
import json
import math
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = "primary-ready-v8-m1-strict-shadow"
MSK = ZoneInfo("Europe/Moscow")


def _read_db(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def summary(pairs):
    counts = Counter(outcome for _, outcome in pairs)
    wins, losses = counts["WIN"], counts["LOSS"]
    priced = [
        row["payout"] / 100 if outcome == "WIN" else -1.0 if outcome == "LOSS" else 0
        for row, outcome in pairs
        if outcome in ("WIN", "LOSS", "DRAW")
        and isinstance(row.get("payout"), (int, float))
        and math.isfinite(row["payout"])
        and 0 <= row["payout"] <= 100
    ]
    return dict(
        opportunities=len(pairs), wins=wins, losses=losses,
        draws=counts["DRAW"], unknown=counts["UNKNOWN"],
        resolved=wins + losses,
        win_rate=wins / (wins + losses) if wins + losses else None,
        priced=len(priced),
        mean_per_trade=sum(priced) / len(priced) if priced else None,
    )


def report(data_dir, since=0, now=None, limit=10000):
    now = time.time() if now is None else now
    path = Path(data_dir) / "ready-candidates.db"
    archive = Path(data_dir) / "market-history.db"
    output = dict(status="no_data", version=VERSION, candidates=0,
                  baseline=summary([]), strict=summary([]), rejected=summary([]),
                  reasons={}, by_symbol={}, pair_n=0, paired_delta_per_opportunity=None,
                  days=0, advantage_confirmed=False, truncated=False)
    if not path.exists():
        return output
    with closing(_read_db(path)) as db:
        rows = [
            json.loads(data) for (data,) in db.execute(
                "SELECT payload FROM candidates WHERE version=? AND "
                "expires_at-entry_at=60 AND observed_at>=? AND observed_at<=? "
                "ORDER BY observed_at DESC LIMIT ?",
                (VERSION, since, now, limit + 1),
            )
        ]
    if len(rows) > limit:
        output["truncated"] = True
        rows = rows[:limit]
    considered = [r for r in rows if r.get("m1_baseline_eligible") is True]
    output["candidates"] = len(considered)
    if not considered:
        return output
    output["status"] = "exploratory"
    canonical = {}
    if archive.exists():
        targets = {(r["symbol"], t) for r in considered
                   for t in (r["entry_at"], r["expires_at"])}
        with closing(_read_db(archive)) as db:
            for symbol, stamp, opening in db.execute(
                "SELECT symbol,time,open FROM bars WHERE time BETWEEN ? AND ? "
                "AND source='otcharts_history' AND revisions=0 "
                "AND first_observed<=? AND last_observed<=?",
                (min(t for _, t in targets), max(t for _, t in targets), now, now),
            ):
                if ((symbol, stamp) in targets
                    and isinstance(opening, (int, float))
                    and math.isfinite(opening) and opening > 0):
                    canonical[(symbol, stamp)] = opening
    baseline, strict, rejected = [], [], []
    breakdown = defaultdict(list)
    paired = []
    reason_counts = Counter()
    for row in considered:
        direction = row.get("direction")
        start = canonical.get((row["symbol"], row["entry_at"]))
        finish = canonical.get((row["symbol"], row["expires_at"]))
        if start is None or finish is None or direction not in ("UP", "DOWN"):
            outcome = "UNKNOWN"
        elif finish == start:
            outcome = "DRAW"
        else:
            outcome = "WIN" if (finish > start) == (direction == "UP") else "LOSS"
        item = (row, outcome)
        baseline.append(item)
        accepted = row.get("m1_strict_eligible") is True
        (strict if accepted else rejected).append(item)
        breakdown[row["symbol"]].append((accepted, item))
        if not accepted:
            reason_counts[row.get("reason", "unknown")] += 1
        payout = row.get("payout")
        if outcome in ("WIN", "LOSS", "DRAW") and isinstance(payout, (int, float)) and math.isfinite(payout) and 0 <= payout <= 100:
            old = payout / 100 if outcome == "WIN" else -1 if outcome == "LOSS" else 0
            paired.append((row["entry_at"], (old if accepted else 0) - old))
    output.update(
        baseline=summary(baseline), strict=summary(strict), rejected=summary(rejected),
        reasons=dict(reason_counts.most_common(12)),
        by_symbol={
            symbol: dict(
                baseline=summary([item for _, item in records]),
                strict=summary([item for accepted, item in records if accepted]),
            )
            for symbol, records in sorted(breakdown.items())
        },
        pair_n=len(paired),
        paired_delta_per_opportunity=(
            sum(delta for _, delta in paired) / len(paired) if paired else None
        ),
        days=len({datetime.fromtimestamp(at, MSK).date() for at, _ in paired}),
    )
    return output


def format_report(data):
    if data["status"] == "no_data":
        return (
            "Строгий M1: пока нет кандидатов, прошедших старые правила. "
            "Нужна новая история после обновления; старые сигналы не пересчитаны."
        )
    def line(label, metrics):
        rate = (f"{metrics['win_rate']:.1%}"
                if metrics["win_rate"] is not None else "—")
        return (f"{label}: {metrics['opportunities']} возможн. · "
                f"WIN {metrics['wins']} / LOSS {metrics['losses']} · "
                f"UNKNOWN {metrics['unknown']} · винрейт {rate}")
    lines = [
        "M1: старые правила против строгого фильтра (одни и те же возможности)",
        line("Старый M1", data["baseline"]),
        line("Строгий M1", data["strict"]),
        line("Отсеяно новым", data["rejected"]),
        f"Сопоставлено с выплатой и ценой: {data['pair_n']} · "
        f"торговых дней: {data['days']}",
        "Доходность: виртуальная по OTCharts; пропуск=0 единиц на исходную возможность.",
    ]
    if data["paired_delta_per_opportunity"] is not None:
        lines.append(
            f"Разница прибыли на возможность (строгий − старый): "
            f"{data['paired_delta_per_opportunity']:+.3f} ед."
        )
    lines.append("Причины пропусков:")
    for reason, n in data["reasons"].items():
        lines.append(f"· {reason}: {n}")
    lines.append("По активам (до 8 по количеству):")
    ranked = sorted(data["by_symbol"].items(),
                    key=lambda x: x[1]["baseline"]["opportunities"], reverse=True)
    for symbol, metrics in ranked[:8]:
        lines.append(f"· {symbol}: "
                     + line("старый", metrics["baseline"]) + "; "
                     + line("строгий", metrics["strict"]))
    lines.append(
        "Это исследование, не реальные сделки Pocket Option. "
        "Точность неизвестна до независимой проверки на новых днях."
    )
    if data["truncated"]:
        lines.append("Отчёт ограничен последними кандидатами; выборка неполная.")
    return "\n".join(lines)
