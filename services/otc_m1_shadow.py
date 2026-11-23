"""Prospective M1 expiry experiment using the unchanged live M2 entry rules."""
import time
from pathlib import Path

from services.otc_bollinger_live import quality_rejection
from services.otc_community_cycles import signals
from services.otc_delivery_guard import check_data
from services.otc_market import symbol_priority
from services.otc_rsi_shadow import RSIShadow
from services.otc_shadow_variants import pattern_directions
from services.otc_strategies_live import evaluate_cycle
from services.otc_validation_report import load
from services.otc_cycle_dashboard import cycle_compact, cycle_block

VERSION = "live-rules-m1-expiry-v1"
METHODS = {"bb_reentry": "S2", "break_retest": "S3", "ema_pullback": "S4", "exhaustion": "S5"}
STRATEGIES = ("S1", "S2", "S3", "S4", "S5")


def candidates(snapshot, now):
    entry = int(now) // 60 * 60 + 60
    if snapshot is None or not 15 <= entry - now <= 20:
        return []
    histories, assets, quotes, end = snapshot
    ranked = {}
    for symbol, minutes in histories.items():
        asset = assets.get(symbol)
        base = dict(symbol=symbol, entry_at=entry, expires_at=entry+180,
                    cycle_seconds=60, feature_asof=entry-60, observed_at=now,
                    gates={"cycle": True})
        if check_data(base, snapshot, now):
            continue
        # 120 selects exactly the production entry rules on closed M1 bars.
        # Only settlement duration is changed to 60; this is not a live M1 signal.
        choices = {METHODS[k]: v for k, v in signals(list(minutes), 120, entry-60).items()
                   if k in METHODS}
        s1 = pattern_directions(list(minutes), 120, entry-60).get("bollinger_reversal")
        if s1 and quality_rejection(s1) is None:
            choices["S1"] = s1
        for strategy, pattern in choices.items():
            row = dict(base, name=asset.name or symbol, payout=asset.payout,
                       version=VERSION+":"+strategy, direction=pattern["direction"],
                       evidence=pattern, experiment=dict(version=VERSION, strategy=strategy,
                       cycle_seconds=60, attempts=3, shadow_only=True, analysis_period=60))
            priority = (symbol_priority(symbol), -asset.payout, symbol)
            if strategy not in ranked or priority < ranked[strategy][0]:
                ranked[strategy] = priority, row
    return [ranked[s][1] for s in sorted(ranked)]


class M1Shadow(RSIShadow):
    filename = "m1-shadow.db"
    label = "S1-S5 M1"

    def candidate_rows(self, snapshot, now):
        return candidates(snapshot, now)


def report(root, strategy="all", now=None):
    now = time.time() if now is None else now
    rows, prices = load(Path(root)/"data"/M1Shadow.filename, 0, now)
    lines = ["🧪 Фон M1 · S1–S5", "С начала теста · одна попытка 1 минута",
             "WR полного цикла · до 3 попыток", ""]
    for code in STRATEGIES:
        if strategy not in ("all", code):
            continue
        outcomes = []
        for row in rows:
            if row.get("version") != VERSION+":"+code or row.get("cycle_seconds") != 60:
                continue
            attempts, _, state = evaluate_cycle(dict(row, seconds=60), prices, now)
            outcomes.append(dict(outcome=state, attempts=attempts))
        lines.append(cycle_compact(code, outcomes) if strategy == "all"
                     else cycle_block(code+" · M1", outcomes, detailed=True))
        lines.append("")
    lines += ["WR = WIN / (WIN + LOSS). Ничьи, ожидание и ❔ исключены.",
              "Повтор только после LOSS. После WIN, ничьей или ❔ — остановка.",
              "Только фоновый тест; пользователям M1 не отправляется.",
              "Правила входа те же, что в основе M2. Результаты M1 считаются отдельно."]
    return "\n".join(lines)
