"""Nine shadow-only variants: three fixed methods by M2/M5/M15."""

import asyncio
import time
from collections import Counter
from statistics import mean, pstdev

from services.otc_candidate_research import CandidateJournal, TickSampler
from services.otc_community_patterns import forecasts
from services.otc_delivery_guard import check_data
from services.otc_ml.preview import aligned_entry
from services.otc_strategy import aggregate
from services.otc_validation_report import load, resolve

VERSION = "community-cycles-v1"
METHODS = {
    "bb_reentry": "Боллинджер · возврат внутрь",
    "break_retest": "Пробой и ретест",
    "ema_pullback": "Откат EMA9/19",
}


def signals(minutes, seconds, asof):
    decisions = {
        k: v for k, v in forecasts(minutes, seconds, asof).items() if k in METHODS
    }
    period = 60 if seconds == 120 else seconds
    bars = aggregate([b for b in minutes if b.time + 60 <= asof], period)[-60:]
    if (
        len(bars) < 60
        or not 0 <= asof - bars[-1].time - period < period
        or any(b.time - a.time != period for a, b in zip(bars, bars[1:]))
    ):
        return decisions
    from services.otc_ml.features import indicators

    if indicators(bars) is None:
        return decisions
    old = [b.close for b in bars[-21:-1]]
    current = [b.close for b in bars[-20:]]
    m0, d0 = mean(old), pstdev(old)
    m1, d1 = mean(current), pstdev(current)
    if d0 > 0 and d1 > 0 and m1 - 2 * d1 < current[-1] < m1 + 2 * d1:
        direction = (
            "UP" if old[-1] < m0 - 2 * d0 else "DOWN" if old[-1] > m0 + 2 * d0 else None
        )
        if direction:
            decisions["bb_reentry"] = dict(
                direction=direction,
                analysis_period=period,
                rule_version="bb-reentry-no-rsi-v1",
            )
    return decisions


def prepare(snapshot, now):
    history, assets, quotes, end = snapshot
    entry = int(now) // 60 * 60 + 60
    if not 15 <= entry - now <= 20:
        return []
    rows = []
    for seconds in (120, 300, 900):
        if not aligned_entry(entry, seconds):
            continue
        for symbol, minutes in history.items():
            asset = assets.get(symbol)
            if asset is None:
                continue
            row = dict(
                symbol=symbol,
                name=getattr(asset, "name", None) or symbol,
                entry_at=entry,
                expires_at=entry + 3 * seconds,
                feature_asof=entry - 60,
                observed_at=now,
                gates={"cycle": True},
                selected=False,
            )
            if check_data(row, snapshot, now):
                continue
            for method, decision in signals(list(minutes), seconds, entry - 60).items():
                rows.append(
                    dict(
                        row,
                        version=method + "-cycles-v1",
                        direction=decision["direction"],
                        payout=asset.payout,
                        evidence={"pattern": decision},
                        experiment=dict(
                            version=VERSION,
                            cycle_seconds=seconds,
                            method=method,
                            attempts=3,
                            same_direction=True,
                        ),
                    )
                )
    return rows


class ShadowCycles:
    def __init__(self, path, clock=time.time):
        self.path = path
        self.journal = CandidateJournal(path)
        self.sampler = TickSampler()
        self.clock = clock
        now = clock()
        records, _ = load(path, 0, now)
        self.sampler.register([r for r in records if r["expires_at"] > now], now)

    def observe(self, tick, now):
        self.sampler.observe(tick, now)

    async def plan(self, snapshot):
        history, assets, quotes, end = snapshot
        frozen = (
            {s: list(b) for s, b in history.items()},
            dict(assets),
            dict(quotes),
            end,
        )
        rows = await asyncio.to_thread(prepare, frozen, self.clock())
        await asyncio.to_thread(self.journal.record, rows)
        self.sampler.register(rows, self.clock())

    async def flush(self):
        samples = self.sampler.drain(self.clock())
        if samples:
            try:
                await asyncio.to_thread(self.journal.samples, samples)
            except Exception:
                self.sampler.restore(samples)
                raise


def report(path, since, now):
    rows, prices = load(path, since, now)
    groups = {
        (method, seconds): Counter()
        for method in METHODS
        for seconds in (120, 300, 900)
    }
    for row in rows:
        experiment = row.get("experiment", {})
        key = experiment.get("method"), experiment.get("cycle_seconds")
        if experiment.get("version") != VERSION or key not in groups:
            continue
        seconds = key[1]
        outcomes = [
            resolve(
                dict(
                    row,
                    entry_at=row["entry_at"] + i * seconds,
                    expires_at=row["entry_at"] + (i + 1) * seconds,
                ),
                prices,
                now,
            )
            for i in range(3)
        ]
        result = next((item for item in outcomes if item != "LOSS"), "LOSS")
        groups[key][result] += 1
        if result == "WIN":
            groups[key]["stage" + str(outcomes.index("WIN") + 1)] += 1
    return [
        dict(method=method, seconds=seconds, counts=dict(counts))
        for (method, seconds), counts in groups.items()
    ]


def text_report(data, seconds):
    lines = [
        f"Фон ×3 · M{seconds // 60}",
        "Три попытки · одно направление · без ставок",
    ]
    for item in data:
        if item["seconds"] != seconds:
            continue
        c = item["counts"]
        w, loss = c.get("WIN", 0), c.get("LOSS", 0)
        rate = f"{100 * w / (w + loss):.1f}%" if w + loss else "—"
        lines += [
            "\n" + METHODS[item["method"]],
            f"WIN {w} · LOSS {loss} · WR {rate}",
            f"① {c.get('stage1', 0)} · ② {c.get('stage2', 0)} · ③ {c.get('stage3', 0)}",
            f"Ничьи {c.get('DRAW', 0)} · ждут {c.get('PENDING', 0)} · нет данных {c.get('UNKNOWN', 0)}",
        ]
    lines.append(
        "\nПосле WIN/DRAW/UNKNOWN цикл остановлен. Исторические одиночные исходы сюда не перенесены."
    )
    return "\n".join(lines)
