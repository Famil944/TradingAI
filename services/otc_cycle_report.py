"""Read-only cycle statistics shared by local, Telegram and public reports."""

import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from services.otc_community_cycles import METHODS
from services.otc_community_cycles import report as shadow_report


def metrics(counts):
    wins, losses = counts.get("WIN", 0), counts.get("LOSS", 0)
    total = sum(counts.get(k, 0) for k in ("WIN", "LOSS", "DRAW", "UNKNOWN", "PENDING"))
    return dict(
        counts=dict(counts),
        total=total,
        resolved=wins + losses,
        win_rate=wins / (wins + losses) if wins + losses else None,
    )


def report(root, since, now):
    root = Path(root)
    groups = {s: Counter() for s in (60, 120, 300, 900)}
    path = root / "bollinger-cycles.db"
    if path.exists():
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            rows = db.execute(
                "SELECT evidence,outcome FROM predictions WHERE sent_at>=? AND sent_at<=? "
                "AND outcome NOT IN ('LATE','CANCELLED','NOT_DELIVERED')",
                (since, now),
            ).fetchall()
        for raw, result in rows:
            plan = json.loads(raw)
            counts = groups.get(plan.get("cycle_seconds"))
            if counts is None:
                continue
            counts[result] += 1
            outcomes = plan.get("attempt_outcomes", [])
            if result == "WIN" and "WIN" in outcomes:
                counts["stage" + str(outcomes.index("WIN") + 1)] += 1
    return dict(
        since=since,
        until=now,
        main=[dict(seconds=s, **metrics(c)) for s, c in groups.items()],
        shadow=[
            dict(method=r["method"], seconds=r["seconds"], **metrics(r["counts"]))
            for r in shadow_report(root / "community-cycles.db", since, now)
        ],
    )


def format_report(data):
    lines = [
        "Циклы ×3 · сохранённые результаты",
        "WR = WIN / (WIN + LOSS); UNKNOWN исключены.",
    ]
    for title, rows in (("Основной Боллинджер", data["main"]), ("Фон", data["shadow"])):
        lines.append("\n" + title)
        for row in rows:
            c = row["counts"]
            name = METHODS[row["method"]] + " · " if "method" in row else ""
            wr = f"{row['win_rate']:.1%}" if row["win_rate"] is not None else "—"
            lines.append(
                f"{name}M{row['seconds'] // 60}: WIN {c.get('WIN', 0)} / LOSS {c.get('LOSS', 0)} · WR {wr} · всего {row['total']}"
            )
            lines.append(
                f"① {c.get('stage1', 0)} · ② {c.get('stage2', 0)} · ③ {c.get('stage3', 0)} · ничьи {c.get('DRAW', 0)} · нет данных {c.get('UNKNOWN', 0)} · ждут {c.get('PENDING', 0)}"
            )
    return "\n".join(lines)
