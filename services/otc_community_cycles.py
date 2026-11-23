"""Shadow-only variants: M2/M5/M15 cycles and two separate M1 methods."""

from services.otc_market import ACTIVE_TIMEFRAMES

import asyncio
import logging
import time
from collections import Counter
from statistics import mean, pstdev

from services.otc_candidate_research import CandidateJournal, TickSampler
from services.otc_community_patterns import forecasts
from services.otc_delivery_guard import check_data
from services.otc_ml.preview import aligned_entry
from services.otc_strategy import aggregate
from services.otc_validation_report import load, resolve

LOG = logging.getLogger(__name__)
VERSION = "community-cycles-v1"
METHODS = {
    "bb_reentry": "Боллинджер · возврат внутрь",
    "break_retest": "Пробой и ретест",
    "ema_pullback": "Откат EMA9/19",
    "exhaustion": "Свой метод · Истощение и отскок",
}


def signals(minutes, seconds, asof):
    decisions = {
        k: v for k, v in forecasts(minutes, seconds, asof).items() if k in METHODS
    }
    if seconds in (60, 120):
        from services.otc_exhaustion_shadow import decision as exhaustion_decision
        custom = exhaustion_decision(minutes, asof)
        if custom is not None:
            decisions["exhaustion"] = custom
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


def prepare(snapshot, now, diagnostic=None):
    history, assets, quotes, end = snapshot
    entry = int(now) // 60 * 60 + 60
    if not 15 <= entry - now <= 20:
        if diagnostic is not None:
            diagnostic["outside_planning_window"] += 1
        return []
    if diagnostic is not None:
        diagnostic["planning_windows"] += 1
    rows = []
    for seconds in ACTIVE_TIMEFRAMES:
        if not aligned_entry(entry, seconds):
            if diagnostic is not None:
                diagnostic["unaligned_timeframe"] += 1
            continue
        if diagnostic is not None:
            diagnostic[f"aligned_M{seconds // 60}"] += 1
        for symbol, minutes in history.items():
            if diagnostic is not None:
                diagnostic["pairs_checked"] += 1
            asset = assets.get(symbol)
            if asset is None:
                if diagnostic is not None:
                    diagnostic["missing_asset"] += 1
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
            data_reason = check_data(row, snapshot, now)
            if data_reason:
                if diagnostic is not None:
                    diagnostic["data_rejected"] += 1
                    diagnostic["data_reason:" + str(data_reason)[:64]] += 1
                continue
            if diagnostic is not None:
                diagnostic["data_passed"] += 1
                diagnostic[f"data_passed_M{seconds // 60}"] += 1
                # Diagnostic only; signal detection itself is unchanged.
                period = 60 if seconds == 120 else seconds
                complete = aggregate(
                    [bar for bar in minutes if bar.time + 60 <= entry - 60],
                    period,
                )
                if len(complete) < 60:
                    diagnostic[f"history_short_M{seconds // 60}"] += 1
                elif any(
                    b.time - a.time != period
                    for a, b in zip(complete[-60:], complete[-59:])
                ):
                    diagnostic[f"history_gap_M{seconds // 60}"] += 1
            decisions = signals(list(minutes), seconds, entry - 60)
            if diagnostic is not None:
                diagnostic["no_pattern" if not decisions else "pattern_found"] += 1
                diagnostic[
                    f"{'pattern_found' if decisions else 'no_pattern'}_M{seconds // 60}"
                ] += 1
            for method, decision in decisions.items():
                if diagnostic is not None:
                    diagnostic["candidate:" + method] += 1
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
        self.diagnostic = Counter()
        self.diagnostic_at = clock()
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
        now = self.clock()
        window_stats = Counter()
        rows = await asyncio.to_thread(prepare, frozen, now, window_stats)
        self.diagnostic.update(window_stats)
        await asyncio.to_thread(self.journal.record, rows)
        self.diagnostic["written_to_journal"] += len(rows)
        if self.clock() - self.diagnostic_at >= 300:
            LOG.info("OTC S2-S5 candidate production diagnostics (last %.0fs): %s",
                     self.clock() - self.diagnostic_at, dict(self.diagnostic))
            self.diagnostic.clear()
            self.diagnostic_at = self.clock()
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
        for seconds in (60, 120, 300, 900)
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
