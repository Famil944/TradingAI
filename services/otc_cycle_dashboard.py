"""Presentation of full three-attempt cycles, never broker trade executions."""
from collections import Counter

from services.otc_public_stats import _attempts, metrics


def cycle_summary(rows):
    summary = metrics(rows)
    stages = Counter()
    for row in rows:
        attempts = _attempts(row.get("attempts", []))
        if row["outcome"] == "WIN" and attempts and attempts[-1] == "WIN":
            if len(attempts) <= 3:
                stages[len(attempts)] += 1
    summary["stages"] = [stages[i] for i in (1, 2, 3)]
    summary["unclassified_wins"] = summary["win"] - sum(stages.values())
    return summary


def cycle_block(title, rows, *, detailed=False):
    m = cycle_summary(rows)
    rate = f"{m['wr']:.1f}%" if m["known"] else "—"
    first, second, third = m["stages"]
    lines = [title, f"WR цикла ×3: {rate} · завершено {m['known']}",
             f"✅ Победы: {m['win']} · ❌ Поражения: {m['loss']}"]
    if detailed:
        lines.extend(["", "Победная попытка:",
                      f"① Первая — {first}", f"② Вторая — {second}", f"③ Третья — {third}"])
    else:
        lines.append(f"Победы: ① {first}  ·  ② {second}  ·  ③ {third}")
    if m["unclassified_wins"]:
        lines.append(f"Победы без номера попытки: {m['unclassified_wins']}")
    lines.append(f"⏳ В работе {m['pending']} · ➖ Ничьи {m['draw']} · ❔ Нет данных {m['unknown']}")
    if detailed:
        lines.extend(["", f"Всего опубликовано: {m['total']}",
                      f"Основа WR: {m['win']} / {m['known']} завершённых WIN/LOSS"])
    return "\n".join(lines)


def cycle_compact(title, rows):
    m = cycle_summary(rows)
    rate = f"{m['wr']:.1f}%" if m["known"] else "—"
    return (f"{title} · {rate} · ✅ {m['win']} / ❌ {m['loss']}"
            f"\n⏳ {m['pending']} · ➖ {m['draw']} · ❔ {m['unknown']}")


def expiry_lines(rows):
    lines = ["По экспирациям · WR полного цикла:"]
    for seconds in (120, 300, 900):
        m = metrics([r for r in rows if r["seconds"] == seconds])
        rate = f"{m['wr']:.1f}%" if m["known"] else "—"
        lines.append(f"M{seconds // 60} · {rate} · ✅ {m['win']} / ❌ {m['loss']} · завершено {m['known']}")
    return "\n".join(lines)
