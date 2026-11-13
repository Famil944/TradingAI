"""Read-only prospective filter/latency report; never selects production thresholds."""

import argparse
import json
import math
import random
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from services.otc_candidate_research import DELAYS, SCHEMA
from services.otc_statistics import wilson

GATES = ("adx", "ema_distance", "macd", "rsi", "bollinger")
POLICIES = (
    ("context", "full", "full_micro")
    + tuple(f"only_{g}" for g in GATES)
    + tuple(f"without_{g}" for g in GATES)
)
MOSCOW = ZoneInfo("Europe/Moscow")


def readonly(path):
    return sqlite3.connect(
        Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2
    )


def accepts(row, policy):
    gates = row.get("gates", {})
    if not all(g in gates for g in GATES):
        return False
    if policy == "context":
        return True
    if policy == "full_micro":
        movement = row.get("micro_move_atr")
        return (
            all(gates.values())
            and isinstance(movement, (int, float))
            and math.isfinite(movement)
            and movement >= -0.25
        )
    if policy == "full":
        return all(gates.values())
    if policy.startswith("only_"):
        return gates[policy.removeprefix("only_")]
    omitted = policy.removeprefix("without_")
    return all(gates[g] for g in GATES if g != omitted)


def outcome(direction, entry, expiry):
    if entry is None or expiry is None:
        return "UNKNOWN"
    delta = expiry - entry
    return (
        "DRAW"
        if delta == 0
        else "WIN"
        if (delta > 0) == (direction == "UP")
        else "LOSS"
    )


def net(row, result):
    payout = row.get("payout")
    if (
        not isinstance(payout, (int, float))
        or not math.isfinite(payout)
        or not 0 <= payout <= 100
        or result not in ("WIN", "LOSS", "DRAW")
    ):
        return None
    return payout / 100 if result == "WIN" else -1.0 if result == "LOSS" else 0.0


def summary(rows, results):
    counts = Counter(results)
    w, loss = counts["WIN"], counts["LOSS"]
    returns = [
        value
        for r, result in zip(rows, results)
        if (value := net(r, result)) is not None
    ]
    return dict(
        n=len(rows),
        outcomes=dict(counts),
        win_rate=w / (w + loss) if w + loss else None,
        wilson_95=wilson(w, loss),
        unknown_share=counts["UNKNOWN"] / len(rows) if rows else None,
        known_payout_results=len(returns),
        mean_net_units=sum(returns) / len(returns) if returns else None,
    )


def paired_day_comparison(rows, outcomes, baseline, alternative):
    """Same scored opportunities, zero for abstention; resample whole MSK days."""
    days = defaultdict(lambda: [0.0, 0])
    missing = 0
    for row, result in zip(rows, outcomes):
        value = net(row, result)
        if value is None:
            missing += 1
            continue
        day = datetime.fromtimestamp(row["entry_at"], MOSCOW).date().isoformat()
        days[day][0] += value * (
            int(accepts(row, alternative)) - int(accepts(row, baseline))
        )
        days[day][1] += 1
    blocks = list(days.values())
    total = sum(n for _, n in blocks)
    interval = None
    if len(blocks) >= 7:
        rng = random.Random(73)
        estimates = []
        for _ in range(500):
            selected = rng.choices(blocks, k=len(blocks))
            estimates.append(sum(v for v, _ in selected) / sum(n for _, n in selected))
        estimates.sort()
        interval = [estimates[12], estimates[487]]
    return dict(
        baseline=baseline,
        alternative=alternative,
        days=len(days),
        paired_opportunities=total,
        excluded_unknown_or_payout=missing,
        delta_per_opportunity=sum(v for v, _ in blocks) / total if total else None,
        day_bootstrap_95=interval,
        automatic_promotion=False,
    )


def report(path, archive_path=None, since=0, now=None):
    now = time.time() if now is None else now
    result = dict(
        schema=SCHEMA,
        generated_at=now,
        since=since,
        status="awaiting_prospective_data",
        candidates=0,
        groups=[],
        limitations=[
            "Shadow opportunities, not delivered signals or broker trades.",
            "Only the current monitored universe and existing context_setup are tested.",
            "Context already contains indicator rules; ablations concern extra v5 gates only.",
            "Policies ignore pending positions/cooldown; returns are per opportunity, not portfolio profit.",
            "Tick delays keep the original expiry fixed; tick and historical sources stay separate.",
            "Payout is the decision snapshot, not confirmed payout at a delayed entry.",
            "Wilson intervals ignore dependence; day bootstrap is exploratory, requires >=7 days.",
            "Many comparisons: confirm a chosen hypothesis on a later untouched period.",
            "Missing ticks, restarts, gaps and revised/noncanonical bars are never interpolated.",
        ],
    )
    if not Path(path).exists():
        return result
    with closing(readonly(path)) as db:
        rows = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT payload FROM candidates WHERE entry_at>=? AND observed_at<=? ORDER BY entry_at,symbol",
                (since, now),
            )
        ]
        samples = {
            (s, target): (at, received, price)
            for s, target, at, received, price in db.execute(
                "SELECT symbol,target,tick_at,received_at,price FROM samples WHERE target>=? AND received_at<=?",
                (since, now),
            )
        }
    result["candidates"] = len(rows)
    result["rejections"] = dict(Counter(r["reason"] for r in rows))
    result["operational_blocks"] = dict(
        Counter(r.get("operational_block") or "none" for r in rows)
    )
    if not rows:
        return result
    result["status"] = "exploratory_only"
    canonical = {}
    if archive_path and Path(archive_path).exists():
        start, end = (
            min(r["entry_at"] for r in rows),
            max(r["expires_at"] for r in rows),
        )
        with closing(readonly(archive_path)) as db:
            canonical = {
                (s, at): price
                for s, at, price in db.execute(
                    "SELECT symbol,time,open FROM bars WHERE time BETWEEN ? AND ? "
                    "AND source='otcharts_history' AND revisions=0 AND first_observed<=? "
                    "AND last_observed<=?",
                    (start, end, now, now),
                )
                if math.isfinite(price) and price > 0
            }

    def sample_price(symbol, target):
        sample = samples.get((symbol, target))
        if sample:
            at, received, price = sample
            if (
                target <= at <= received <= target + 2
                and math.isfinite(price)
                and price > 0
            ):
                return price
        return None

    def resolve(row, source, delay):
        if now < row["expires_at"] + (60 if source == "historical_open" else 2):
            return "PENDING"
        if source == "historical_open":
            entry = canonical.get((row["symbol"], row["entry_at"]))
            expiry = canonical.get((row["symbol"], row["expires_at"]))
        else:
            entry = sample_price(row["symbol"], row["entry_at"] + delay)
            expiry = sample_price(row["symbol"], row["expires_at"])
        return outcome(row["direction"], entry, expiry)

    cohorts = defaultdict(list)
    for row in rows:
        if accepts(row, "context") or row.get("gates", {}).get("min_confirmation"):
            cohorts[(row["version"], row["expires_at"] - row["entry_at"])].append(row)
    for (version, horizon), cohort in sorted(cohorts.items()):
        if version == "primary-ready-v6-one-confirmation":
            results = [resolve(r, "historical_open", 0) for r in cohort]
            result["groups"].append(
                dict(
                    version=version,
                    seconds=horizon,
                    confirmation_policy=True,
                    candidates=summary(cohort, results),
                    counts={
                        str(n): summary(
                            [r for r in cohort if r.get("confirmation_count") == n],
                            [
                                v
                                for r, v in zip(cohort, results)
                                if r.get("confirmation_count") == n
                            ],
                        )
                        for n in range(1, 6)
                    },
                    delays={
                        str(d): summary(
                            cohort, [resolve(r, "observed_tick", d) for r in cohort]
                        )
                        for d in DELAYS
                    },
                )
            )
            continue
        group = dict(
            version=version,
            seconds=horizon,
            context_candidates=len(cohort),
            micro_observed=sum("micro_move_atr" in r for r in cohort),
            policies={},
            paired_filter_comparisons=[],
            segments={},
            delays={},
        )
        historic = [resolve(r, "historical_open", 0) for r in cohort]
        for policy in POLICIES:
            indices = [i for i, r in enumerate(cohort) if accepts(r, policy)]
            chosen = [cohort[i] for i in indices]
            # Rank each minute using exactly the original score and tie breaker.
            best = {}
            for r in chosen:
                key = r["entry_at"]
                if key not in best or (r["quality_score"], r["symbol"]) > (
                    best[key]["quality_score"],
                    best[key]["symbol"],
                ):
                    best[key] = r
            ranked = list(best.values())
            group["policies"][policy] = dict(
                coverage=len(chosen) / len(cohort),
                historical_open=summary(chosen, [historic[i] for i in indices]),
                top_per_minute=summary(
                    ranked, [resolve(r, "historical_open", 0) for r in ranked]
                ),
            )
            if policy != "full":
                group["paired_filter_comparisons"].append(
                    paired_day_comparison(cohort, historic, "full", policy)
                )
        chosen = [r for r in cohort if accepts(r, "full")]
        # Delay comparisons use the same complete cohort for every delay.
        complete = [
            r
            for r in chosen
            if all(
                resolve(r, "observed_tick", d) in ("WIN", "LOSS", "DRAW")
                for d in DELAYS
            )
        ]
        group["paired_latency_candidates"] = len(complete)
        for delay in DELAYS:
            group["delays"][str(delay)] = dict(
                all=summary(
                    chosen, [resolve(r, "observed_tick", delay) for r in chosen]
                ),
                paired=summary(
                    complete, [resolve(r, "observed_tick", delay) for r in complete]
                ),
            )
        for field in ("symbol", "regime", "hour_msk"):
            subsets = defaultdict(list)
            for r in chosen:
                key = (
                    str(datetime.fromtimestamp(r["entry_at"], MOSCOW).hour)
                    if field == "hour_msk"
                    else r.get(field, "unknown")
                )
                subsets[key].append(r)
            group["segments"][field] = {
                key: summary(values, [resolve(r, "historical_open", 0) for r in values])
                for key, values in sorted(subsets.items())
            }
        result["groups"].append(group)
    return result


def format_report(data):
    lines = [
        "Исследование Основного · теневые кандидаты",
        f"Кандидатов: {data['candidates']} · {data['status']}",
        "Виртуальные исходы; преимущество не подтверждено.",
    ]
    for group in data["groups"]:
        lines.append(f"\n{group['version']} · M{group['seconds'] // 60}")
        if group.get("confirmation_policy"):
            for n, item in group["counts"].items():
                rate = (
                    f"{100 * item['win_rate']:.1f}%"
                    if item["win_rate"] is not None
                    else "—"
                )
                lines.append(f"Совпало {n}/5: n={item['n']}, WR={rate}")
            continue
        for policy in ("context", "full", "full_micro"):
            item = group["policies"][policy]["historical_open"]
            rate = (
                f"{100 * item['win_rate']:.1f}%"
                if item["win_rate"] is not None
                else "—"
            )
            lines.append(
                f"{policy}: n={item['n']}, WR={rate}, неизвестно={item['outcomes'].get('UNKNOWN', 0)}, ожидают={item['outcomes'].get('PENDING', 0)}"
            )
        lines.append(
            f"Общая выборка для задержек: {group['paired_latency_candidates']}"
        )
        for delay, stats in group["delays"].items():
            item = stats["paired"]
            rate = (
                f"{100 * item['win_rate']:.1f}%"
                if item["win_rate"] is not None
                else "—"
            )
            lines.append(f"Вход +{delay} с: WR={rate}")
    lines.append(
        "Фильтры и пороги автоматически не меняются. Полный JSON: локальный отчёт otc_candidate_report."
    )
    return "\n".join(lines)


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--journal", type=Path, default=root / "data/ready-candidates.db"
    )
    parser.add_argument("--archive", type=Path, default=root / "data/market-history.db")
    parser.add_argument("--since", type=float, default=0, help="UTC epoch seconds")
    parser.add_argument(
        "--until", type=float, help="Evidence cutoff, UTC epoch seconds"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.until is not None and args.until < args.since:
        parser.error("--until must be >= --since")
    data = report(args.journal, args.archive, args.since, args.until)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
    print(format_report(data))


if __name__ == "__main__":
    main()
