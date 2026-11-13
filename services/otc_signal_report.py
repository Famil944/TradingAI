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
    sources = Counter(
        (row["result_source"] or "legacy_unattributed")
        for row in rows
        if row["outcome"] in ("WIN", "LOSS", "DRAW", "UNKNOWN")
        and "result_source" in row.keys()
    )
    source_text = ", ".join(
        f"{name}: {n}" for name, n in sorted(sources.items())
    ) or "нет атрибуции"
    return (
        f"{len(rows)} сигн. · +{wins} / −{losses} · {rate} · итог {result}"
        f" · ничьи {counts['DRAW']} · без исхода {len(rows) - wins - losses - counts['DRAW']}"
        f" · происхождение: {source_text}"
    )


def read_journal(journal, since, until):
    with closing(sqlite3.connect(journal.path, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            """SELECT symbol, entry_at, expires_at, direction, payout, outcome, evidence, result_source
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

    for left, right in (("Основной", "Groq"), ("Основной", "XGBoost"),
                        ("Groq", "XGBoost")):
        other = {key(row): row for row in sources.get(right, [])}
        matched = [(row, other[key(row)]) for row in sources.get(left, [])
                   if key(row) in other]
        same = [a for a, b in matched if a["direction"] == b["direction"]]
        different = [a for a, b in matched if a["direction"] != b["direction"]]
        lines.append(
            f"\n{left} / {right} · одинаковый актив, вход и экспирация: "
            f"пересечений {len(matched)}, согласны {len(same)}, против {len(different)}"
        )
        if matched:
            lines.append(f"Согласованные · {summarize(same)}")
            lines.append(f"Противоположные · {summarize(different)}")
        else:
            lines.append("Нет одинаковых возможностей входа; сравнивать точность напрямую нельзя.")
    lines.append(
        "Результаты виртуальные по OTCharts; сравнение не является независимым доказательством доходности."
    )
    return lines
