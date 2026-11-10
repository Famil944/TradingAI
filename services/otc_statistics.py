"""Research metrics; excludes UNKNOWN and pending from resolved performance."""

import math


def wilson(wins, losses):
    n = wins + losses
    if not n:
        return None
    z = 1.959963984540054
    p = wins / n
    middle = p + z * z / (2 * n)
    width = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((middle - width) / (1 + z * z / n), (middle + width) / (1 + z * z / n))


def metrics(results):
    total = len(results)
    wins = sum(r["outcome"] == "WIN" for r in results)
    losses = sum(r["outcome"] == "LOSS" for r in results)
    unknown = sum(r["outcome"] == "UNKNOWN" for r in results)
    equity = peak = drawdown = 0.0
    resolved = 0
    for row in results:
        if row["outcome"] not in ("WIN", "LOSS", "DRAW"):
            continue
        resolved += 1
        equity += (
            row["payout"] / 100
            if row["outcome"] == "WIN"
            else -1
            if row["outcome"] == "LOSS"
            else 0
        )
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return dict(
        total=total,
        wins=wins,
        losses=losses,
        unknown=unknown,
        unknown_share=unknown / total if total else None,
        win_rate=wins / (wins + losses) if wins + losses else None,
        wilson_95=wilson(wins, losses),
        expectancy=equity / resolved if resolved else None,
        max_drawdown_units=drawdown,
        net_units=equity,
    )


def format_stats(rows, period):
    lines = [
        f"Виртуальная статистика · {period}",
        "ЭКСПЕРИМЕНТАЛЬНЫЙ РЕЖИМ. Реальные сделки не учитываются.",
    ]
    if not rows:
        return "\n".join(lines + ["Сигналов пока нет."])
    for r in rows[:30]:
        ci = wilson(r["wins"], r["losses"])
        resolved = r["wins"] + r["losses"]
        rate = f"{100 * r['wins'] / resolved:.1f}%" if resolved else "нет данных"
        interval = f"{100 * ci[0]:.1f}–{100 * ci[1]:.1f}%" if ci else "нет данных"
        expectancy = (
            f"{r['expectancy']:+.3f}" if r["expectancy"] is not None else "нет данных"
        )
        lines.append(
            f"{r['symbol']} M{r['timeframe'] // 60} {r['strategy']}: n={r['total']}, WIN {r['wins']}, LOSS {r['losses']}, DRAW {r['draws']}, UNKNOWN {r['unknown']} ({100 * r['unknown'] / r['total']:.1f}%), ожидают {r['pending']}; win rate {rate}, 95% ДИ {interval}; EV {expectancy} ед./сигнал."
        )
    if len(rows) > 30:
        lines.append("Показаны первые 30 групп. Полный журнал — в SQLite.")
    return "\n".join(lines)[:4000]
