"""Compare delivered test signals using their recorded outcomes and payouts."""

import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo


def summarize(rows):
    counts = Counter(row["outcome"] for row in rows)
    wins, losses = counts["WIN"], counts["LOSS"]
    rate = f"{wins / (wins + losses):.1%}" if wins + losses else "—"
    net = [
        {"WIN": row["payout"] / 100, "LOSS": -1, "DRAW": 0}[row["outcome"]]
        for row in rows
        if row["outcome"] in {"WIN", "LOSS", "DRAW"}
        and row["payout"] is not None
        and 0 <= row["payout"] <= 100
    ]
    result = f"{sum(net):+.2f} ед. (n={len(net)})" if net else "—"
    return (
        f"{len(rows)} сигн. · +{wins} / −{losses} · {rate} · условный итог {result}"
        f" · ничьи {counts['DRAW']} · без исхода {len(rows) - wins - losses - counts['DRAW']}"
    )


def read_journal(journal, since, until):
    with closing(sqlite3.connect(journal.path, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            """SELECT id, symbol, entry_at, expires_at, direction, payout, outcome, evidence,
                      model_sha, sent_at, resolved_at
               FROM predictions WHERE sent_at >= ? AND sent_at < ?
               AND outcome NOT IN ('LATE', 'CANCELLED', 'NOT_DELIVERED')
               ORDER BY entry_at, sent_at, id""",
            (since, until),
        ).fetchall()


def version_key(row):
    payload = json.loads(row["evidence"] or "{}")
    return (row["model_sha"] or "unknown", payload.get("execution_policy", "legacy"))


def journal_stats(journal, seconds=None, since=0, until=None):
    """Use exactly the same delivered-signal population as the menu counter."""
    import time

    until = time.time() if until is None else until
    rows = [
        r
        for r in read_journal(journal, since, until)
        if seconds is None or r["expires_at"] - r["entry_at"] == seconds
    ]
    counts = Counter(r["outcome"] for r in rows)
    wins, losses = counts["WIN"], counts["LOSS"]
    rate = f"{100 * wins / (wins + losses):.1f}%" if wins + losses else "—"
    label = "Основной" if journal.label.startswith("Основной") else journal.label
    title = label + (f" · M{seconds // 60}" if seconds else " · все экспирации")
    lines = [
        title,
        f"Отправлено вовремя: {len(rows)}",
        f"Вин {wins} (+) · Лос {losses} (−)",
        f"Ничьи {counts['DRAW']} · Ожидают {counts['PENDING']} · Нет данных {counts['UNKNOWN']}",
        f"Винрейт {rate} · только WIN / (WIN + LOSS)",
    ]
    other = sum(
        n
        for state, n in counts.items()
        if state not in {"WIN", "LOSS", "DRAW", "PENDING", "UNKNOWN"}
    )
    if other:
        lines.append(f"Другие статусы: {other}")
    overdue = sum(
        r["outcome"] == "PENDING" and until >= r["expires_at"] + 180 for r in rows
    )
    if overdue:
        lines.append(f"Задержан расчёт: {overdue} (в составе ожидающих)")
    with closing(sqlite3.connect(journal.path, timeout=3)) as db:
        excluded = dict(
            db.execute(
                "SELECT outcome,count(*) FROM predictions WHERE created_at>=? AND created_at<? "
                "AND (? IS NULL OR expires_at-entry_at=?) "
                "AND outcome IN ('CANCELLED','NOT_DELIVERED','LATE') GROUP BY outcome",
                (since, until, seconds, seconds),
            ).fetchall()
        )
    lines.append(
        f"Вне счётчика: отменено {excluded.get('CANCELLED', 0)}, не доставлено {excluded.get('NOT_DELIVERED', 0)}, опоздало {excluded.get('LATE', 0)}"
    )
    groups = defaultdict(list)
    for row in rows:
        groups[version_key(row)].append(row)
    lines.append("\nПо версиям (общий итог выше смешивает версии):")
    # Keep one Telegram message bounded; the detailed quality report has all groups.
    for (version, policy), group in list(groups.items())[-8:]:
        lines.append(f"{version[:48]} · {policy[:48]}: {summarize(group)}")
    if len(groups) > 8:
        lines.append(f"Ещё версий: {len(groups) - 8}; полный список — /otc_quality")
    lines.append(
        "Исходы по OTCharts; сопоставимость источника подтверждена пользователем.\nЦиклы ×4 считаются отдельно и не являются винрейтом отдельных сигналов."
    )
    start = (
        datetime.fromtimestamp(since, ZoneInfo("Europe/Moscow")).strftime(
            "%d.%m.%Y %H:%M"
        )
        if since
        else "начала истории"
    )
    lines.append(
        f"По времени отправки, с {start}{' МСК' if since else ''}. Условный итог — ставка 1 на каждый сигнал."
    )
    return "\n".join(lines)


def report(journals, since, until):
    sources = {
        label: read_journal(journal, since, until)
        for label, journal in journals.items()
    }
    lines = [
        "Качество тестовых сигналов · сегодня по МСК",
        "Условный итог: ставка 1 на каждый сигнал, записанная выплата. Сопоставимость OTCharts с платформой подтверждена пользователем.",
    ]
    for label, rows in sources.items():
        lines.append(f"\n{label} · {summarize(rows)}")
        groups = defaultdict(list)
        for row in rows:
            confirmations = json.loads(row["evidence"]).get("confirmations") or {}
            count = (
                confirmations.get("count") if confirmations.get("available") else None
            )
            seconds = int(row["expires_at"] - row["entry_at"])
            groups[*version_key(row), seconds, count].append(row)
        for (version, policy, seconds, count), group in sorted(
            groups.items(),
            key=lambda item: (item[0][0], item[0][1] is None, item[0][1]),
        ):
            lines.append(
                f"{version[:48]} · {policy[:48]} · M{seconds // 60} · инд. {count if count is not None else '—'} · {summarize(group)}"
            )
        contexts = defaultdict(list)
        missing_context = 0
        for row in rows:
            snapshot = json.loads(row["evidence"]).get("confirmations") or {}
            context = snapshot.get("market_context") or {}
            if not context.get("available"):
                missing_context += 1
                continue
            state = snapshot.get("market_state", {}).get("label", "неизвестно")
            seconds = int(row["expires_at"] - row["entry_at"])
            contexts[
                (
                    *version_key(row),
                    seconds,
                    context["version"],
                    state,
                    context["volatility"],
                    context["location"],
                    context["event"],
                )
            ].append(row)
        if contexts:
            lines.append(
                "Контекст рынка · описательные группы, преимущество не доказано:"
            )
            for (
                version,
                policy,
                seconds,
                context_version,
                state,
                volatility,
                location,
                event,
            ), group in contexts.items():
                lines.append(
                    f"{version[:48]} · {policy[:48]} · {context_version} · M{seconds // 60} · {state} · {volatility} · {location} · {event}: {summarize(group)}"
                )
        lines.append(
            f"Без сохранённого контекста: {missing_context}; задним числом не восстановлен."
        )

    def key(row):
        return row["symbol"], row["entry_at"], row["expires_at"]

    groq = {key(row): row for row in sources.get("Groq", [])}
    pairs = defaultdict(list)
    for row in sources.get("Основной", []):
        other = groq.get(key(row))
        if other is not None:
            pairs[row["direction"] == other["direction"]].append(row)
    lines.extend(
        [
            "\nОсновной и Groq · один актив, вход и экспирация",
            f"Совпали · {summarize(pairs[True])}",
            f"Разошлись · {summarize(pairs[False])}",
            "В сравнении показан исход Основного. Винрейт = победы / (победы + поражения).",
        ]
    )
    return lines
