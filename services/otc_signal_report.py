"""Compare delivered test signals using their recorded outcomes and payouts."""

import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing


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
        f"{len(rows)} сигн. · +{wins} / −{losses} · {rate} · итог {result}"
        f" · ничьи {counts['DRAW']} · без исхода {len(rows) - wins - losses - counts['DRAW']}"
    )


def read_journal(journal, since, until):
    with closing(sqlite3.connect(journal.path, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            """SELECT symbol, entry_at, expires_at, direction, payout, outcome, evidence
               FROM predictions WHERE sent_at >= ? AND sent_at < ?
               AND outcome NOT IN ('LATE', 'CANCELLED', 'NOT_DELIVERED')""",
            (since, until),
        ).fetchall()


def report(journals, since, until):
    sources = {label: read_journal(journal, since, until) for label, journal in journals.items()}
    lines = ["Качество тестовых сигналов · сегодня по МСК"]
    for label, rows in sources.items():
        lines.append(f"\n{label} · {summarize(rows)}")
        groups = defaultdict(list)
        for row in rows:
            confirmations = json.loads(row["evidence"]).get("confirmations") or {}
            count = confirmations.get("count") if confirmations.get("available") else None
            seconds = int(row["expires_at"] - row["entry_at"])
            groups[seconds, count].append(row)
        for (seconds, count), group in sorted(
            groups.items(), key=lambda item: (item[0][0], item[0][1] is None, item[0][1])
        ):
            lines.append(f"M{seconds // 60} · инд. {count if count is not None else '—'} · {summarize(group)}")

    def key(row):
        return row["symbol"], row["entry_at"], row["expires_at"]

    groq = {key(row): row for row in sources.get("Groq", [])}
    pairs = defaultdict(list)
    for row in sources.get("Основной", []):
        other = groq.get(key(row))
        if other is not None:
            pairs[row["direction"] == other["direction"]].append(row)
    lines.extend([
        "\nОсновной и Groq · один актив, вход и экспирация",
        f"Совпали · {summarize(pairs[True])}",
        f"Разошлись · {summarize(pairs[False])}",
        "В сравнении показан исход Основного. Винрейт = победы / (победы + поражения).",
    ])
    return lines
