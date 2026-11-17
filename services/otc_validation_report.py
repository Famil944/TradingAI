"""Read-only prospective audits. Tick outcomes and delivered outcomes stay separate."""

import json
import math
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from services.otc_candidate_report import outcome, summary
from services.otc_readiness import downtime


def load(path, since, now):
    if not path.exists():
        return [], {}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT payload FROM candidates WHERE observed_at>=? AND observed_at<=? AND observed_at<entry_at ORDER BY entry_at,symbol",
                (since, now),
            )
        ]
        prices = {
            (s, t): p
            for s, t, at, received, p in db.execute(
                "SELECT symbol,target,tick_at,received_at,price FROM samples WHERE target>=? AND received_at<=?",
                (since, now),
            )
            if t <= at <= received <= t + 2 and math.isfinite(p) and p > 0
        }
    return rows, prices


def resolve(row, prices, now):
    if now < row["expires_at"] + 2:
        return "PENDING"
    return outcome(
        row["direction"],
        prices.get((row["symbol"], row["entry_at"])),
        prices.get((row["symbol"], row["expires_at"])),
    )


def replay(rows, prices, now):
    """Single-method portfolio: one position per horizon, 60s settlement + 60s pause.

    Future outcomes affect selection only after their settlement time. No external
    source competition or Telegram delivery simulation; compare actual sends separately.
    """
    busy = {}
    losses = []
    claimed = set()
    selected, rejected = [], Counter()
    ordered = sorted(
        rows,
        key=lambda r: (
            r["entry_at"],
            -r.get("quality_score", 0),
            r["expires_at"] - r["entry_at"],
            r["symbol"],
        ),
    )
    for row in ordered:
        at = row["observed_at"]
        horizon = row["expires_at"] - row["entry_at"]
        if at < busy.get(horizon, 0):
            rejected["pending_or_result_pause"] += 1
            continue
        if any(
            symbol == row["symbol"] and known <= at < known + 300
            for symbol, known in losses
        ):
            rejected["symbol_loss_pause"] += 1
            continue
        key = row["symbol"], row["entry_at"]
        if key in claimed:
            rejected["duplicate_pair_entry"] += 1
            continue
        selected.append(row)
        claimed.add(key)
        result = resolve(row, prices, now)
        settled = row["expires_at"] + (180 if result == "UNKNOWN" else 60)
        busy[horizon] = settled + 60
        if result == "LOSS":
            losses.append((row["symbol"], settled))
    return selected, dict(rejected)


def report(root, since, now):
    rows, prices = load(root / "ready-candidates.db", since, now)
    groups = defaultdict(list)
    for row in rows:
        experiment = row.get("experiment") or {}
        if experiment.get("cycle_seconds") or row.get("direction") not in (
            "UP",
            "DOWN",
        ):
            continue
        for policy, flag in experiment.get("masks", {}).items():
            if flag is True and policy != "baseline":
                groups[row["version"], policy].append(row)
    result = dict(
        since=since,
        until=now,
        comparisons=[],
        daily=[],
        xgboost=[],
        calibration=[],
        delivered=[],
        downtime=downtime(root / "runtime-readiness.db", since, now),
    )
    for (version, policy), group in groups.items():
        chosen, blocked = replay(group, prices, now)
        for seconds in sorted({r["expires_at"] - r["entry_at"] for r in group}):
            raw = [r for r in group if r["expires_at"] - r["entry_at"] == seconds]
            limited = [r for r in chosen if r["expires_at"] - r["entry_at"] == seconds]
            result["comparisons"].append(
                dict(
                    version=version,
                    policy=policy,
                    seconds=seconds,
                    all=summary(raw, [resolve(r, prices, now) for r in raw]),
                    limited=summary(
                        limited, [resolve(r, prices, now) for r in limited]
                    ),
                    blocked=blocked,
                )
            )
            by_day = defaultdict(list)
            for row in limited:
                by_day[
                    datetime.fromtimestamp(row["entry_at"], ZoneInfo("Europe/Moscow"))
                    .date()
                    .isoformat()
                ].append(row)
            for day, day_rows in sorted(by_day.items()):
                result["daily"].append(
                    dict(
                        day=day,
                        version=version,
                        policy=policy,
                        seconds=seconds,
                        **summary(
                            day_rows, [resolve(r, prices, now) for r in day_rows]
                        ),
                    )
                )
    xrows, xprices = load(root / "xgboost-candidates.db", since, now)
    xgroups, bins = defaultdict(list), defaultdict(list)
    for row in xrows:
        version = (
            row.get("model_sha", "unknown"),
            row.get("calibration_sha", "unknown"),
            row.get("selection_policy", "unknown"),
        )
        seconds = row["expires_at"] - row["entry_at"]
        reason = row.get("operational_block") or row.get("reason", "unknown")
        xgroups[version, seconds, reason].append(row)
        resolved = resolve(row, xprices, now)
        if resolved in ("WIN", "LOSS", "DRAW"):
            p = row["probabilities"][2 if row["direction"] == "UP" else 0]
            bins[version, seconds, min(9, int(p * 10))].append((p, resolved))
    for (version, seconds, reason), group in xgroups.items():
        result["xgboost"].append(
            dict(
                version=version,
                seconds=seconds,
                reason=reason,
                **summary(group, [resolve(r, xprices, now) for r in group]),
            )
        )
    for (version, seconds, bucket), pairs in bins.items():
        result["calibration"].append(
            dict(
                version=version,
                seconds=seconds,
                bin=bucket,
                n=len(pairs),
                predicted=sum(p for p, o in pairs) / len(pairs),
                observed=sum(o == "WIN" for p, o in pairs) / len(pairs),
            )
        )
    for name in ("primary-ready", "primary-not-extended", "ml-preview", "groq-preview"):
        path = root / (name + ".db")
        if not path.exists():
            continue
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            db.row_factory = sqlite3.Row
            delivered = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM predictions WHERE sent_at>=? AND sent_at<=? AND outcome NOT IN ('LATE','CANCELLED','NOT_DELIVERED')",
                    (since, now),
                )
            ]
        daily = defaultdict(list)
        for row in delivered:
            payload = json.loads(row["evidence"])
            version = (
                row["model_sha"],
                payload.get("selection_policy")
                or payload.get("evidence", {}).get("selection_policy", "legacy"),
            )
            day = (
                datetime.fromtimestamp(row["entry_at"], ZoneInfo("Europe/Moscow"))
                .date()
                .isoformat()
            )
            daily[version, day, row["expires_at"] - row["entry_at"]].append(row)
        for (version, day, seconds), group in daily.items():
            result["delivered"].append(
                dict(
                    source=name,
                    version=version,
                    day=day,
                    seconds=seconds,
                    **summary(group, [r["outcome"] for r in group]),
                )
            )
    return result


def format_report(data):
    from services.otc_community_patterns import LABELS

    def score(item):
        c = item["outcomes"]
        rate = f"{100 * item['win_rate']:.1f}%" if item["win_rate"] is not None else "—"
        return f"{c.get('WIN', 0)}/{c.get('LOSS', 0)} · WR {rate} · ничьи {c.get('DRAW', 0)} · ждут {c.get('PENDING', 0)} · нет исхода {c.get('UNKNOWN', 0)}"

    def name(policy):
        return LABELS.get(
            policy, {"strict_primary": "Совпадение M5"}.get(policy, policy)
        )

    gap = data["downtime"]
    lines = [
        "Качество и проверка по дням",
        f"Наблюдаемые разрывы/прогрев: {gap['intervals']} · {gap['seconds'] / 60:.1f} мин · сейчас {'восстановление' if gap['ongoing'] else 'нет открытого разрыва'}",
        "Паузы: одна позиция на экспирацию, 60 с на расчёт + 60 с после исхода, 5 мин на пару после LOSS. Конкуренция других ботов и доставка Telegram не моделируются.",
    ]
    for item in data["comparisons"]:
        lines.append(
            f"\n{name(item['policy'])} M{item['seconds'] // 60} · {item['version']}\nВсе: {score(item['all'])}\nС ограничениями: {score(item['limited'])}"
        )
    lines.append(
        "\nПо дням МСК · фон с ограничениями (фиксированные версии, без автоматического изменения порогов):"
    )
    for item in sorted(
        data["daily"], key=lambda r: (r["day"], r["policy"], r["seconds"])
    ):
        lines.append(
            f"{item['day']} {name(item['policy'])} M{item['seconds'] // 60} [{item['version']}]: {score(item)}"
        )
    lines.append("\nФактически отправленные · отдельные исходы из журналов:")
    for item in data["delivered"]:
        lines.append(
            f"{item['day']} {item['source']} M{item['seconds'] // 60} [{item['version'][0][:36]} / {item['version'][1]}]: {score(item)}"
        )
    lines.append("\nXGBoost · причины отбора и пропуска (модель/калибровка):")
    for item in data["xgboost"]:
        lines.append(
            f"M{item['seconds'] // 60} {item['reason']} [{item['version'][0][:8]}/{item['version'][1][:8]}]: {score(item)}"
        )
    for item in data["calibration"]:
        lines.append(
            f"M{item['seconds'] // 60} [{item['version'][0][:8]}/{item['version'][1][:8]}] оценка {item['bin'] * 10}–{(item['bin'] + 1) * 10}%: прогноз {100 * item['predicted']:.1f}%, факт WIN {100 * item['observed']:.1f}% из {item['n']} (ничьи включены в знаменатель)"
        )
    lines.append(
        "Новый день — следующий временной срез, не гарантия независимости. Мало наблюдений/пропуски не доказывают преимущество. Старые прогнозы задним числом не создаются."
    )
    return "\n".join(lines)
