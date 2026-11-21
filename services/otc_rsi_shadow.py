"""RSI(2)/SMA200 on closed M1 candles, prospective shadow only."""
import asyncio
import logging
import math
import time
from collections import Counter
from pathlib import Path
from statistics import mean

from services.otc_candidate_research import TickSampler
from services.otc_delivery_guard import check_data
from services.otc_early_lead import EarlyJournal, EarlyLeadObserver
from services.otc_groq import fresh
from services.otc_ml.preview import aligned_entry
from services.otc_strategy import rsi
from services.otc_strategies_live import evaluate_cycle
from services.otc_validation_report import load

VERSION = "rsi2-sma200-m1-v1"
PERIODS = (120, 300, 900)
LOG = logging.getLogger(__name__)


def decision(minutes, asof):
    bars = [b for b in minutes if b.time + 60 <= asof][-250:]
    if len(bars) < 201 or bars[-1].time + 60 != asof:
        return None
    if any(b.time - a.time != 60 for a, b in zip(bars, bars[1:])):
        return None
    if any(not all(math.isfinite(v) and v > 0 for v in (b.open, b.high, b.low, b.close))
           or not b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high for b in bars):
        return None
    closes = [b.close for b in bars]
    before, current = rsi(closes[:-1], 2), rsi(closes, 2)
    average = mean(closes[-200:])
    direction = None
    if closes[-1] > average and before >= 5 > current:
        direction = "UP"
    elif closes[-1] < average and before <= 95 < current:
        direction = "DOWN"
    if direction is None:
        return None
    return dict(direction=direction, rsi_before=before, rsi=current,
                sma200=average, close=closes[-1], last_closed=bars[-1].time,
                analysis_period=60)


def candidates(snapshot, now):
    entry = int(now) // 60 * 60 + 60
    if not 15 <= entry - now <= 20:
        return []
    histories, assets, quotes, end = snapshot
    ranked = {}
    for symbol, minutes in histories.items():
        asset = assets.get(symbol)
        if not fresh(asset, quotes.get(symbol), now):
            continue
        evidence = decision(minutes, entry - 60)
        if evidence is None:
            continue
        for seconds in PERIODS:
            if not aligned_entry(entry, seconds) or (end and entry + 3 * seconds + 2 > end):
                continue
            row = dict(symbol=symbol, name=asset.name or symbol, direction=evidence['direction'],
                       entry_at=entry, expires_at=entry+3*seconds, cycle_seconds=seconds,
                       observed_at=now, feature_asof=entry-60, payout=asset.payout,
                       version=VERSION, evidence=evidence, gates={"cycle": True},
                       experiment=dict(strategy=VERSION, version=VERSION,
                                       cycle_seconds=seconds, shadow_only=True))
            if check_data(row, snapshot, now):
                continue
            priority = (-asset.payout, symbol)
            if seconds not in ranked or priority < ranked[seconds][0]:
                ranked[seconds] = priority, row
    return [v[1] for _, v in sorted(ranked.items())]


class RSIShadow(EarlyLeadObserver):
    def __init__(self, root, clock=time.time):
        self.root, self.clock = Path(root), clock
        self.journal = EarlyJournal(self.root / 'data' / 'rsi2-shadow.db')
        self.sampler = TickSampler()
        self.snapshot = None
        self.last_entry = None
        self.next_flush = 0
        self.sampler.register(self.journal.pending_future(clock()), clock())

    async def tick(self, session, cycles):
        now = self.clock()
        if now >= self.next_flush:
            self.next_flush = now + 2
            await self.flush()
        entry = int(now) // 60 * 60 + 60
        if not (session.running() and cycles.enabled and session.transport == 'live'
                and self.snapshot is not None and 15 <= entry-now <= 20):
            return
        if self.last_entry == entry:
            return
        self.last_entry = entry
        h, a, q, end = self.snapshot
        frozen = ({s: list(b) for s, b in h.items()}, dict(a), dict(q), end)
        rows = await asyncio.to_thread(candidates, frozen, now)
        actual = self.clock()
        if not 15 <= entry-actual <= 20:
            return
        rows = [dict(r, observed_at=actual) for r in rows if check_data(r, self.snapshot, actual) is None]
        accepted = await asyncio.to_thread(self.journal.record, rows)
        self.sampler.register(accepted, self.clock())
        LOG.info('RSI2 shadow scan: candidates=%d recorded=%d', len(rows), len(accepted))


def report(root, now=None):
    root = Path(root)
    now = time.time() if now is None else now
    rows, prices = load(root / 'data' / 'rsi2-shadow.db', 0, now)
    lines = ['🧪 Новый фон · RSI(2) + SMA200', 'Анализ M1 · экспирации M2/M5/M15',
             'С начала теста · прогнозы, не сделки', '']
    for seconds in PERIODS:
        subset = [r for r in rows if r['version'] == VERSION and r['cycle_seconds'] == seconds]
        counts, first, stages = Counter(), Counter(), Counter()
        for row in subset:
            attempts, _, result = evaluate_cycle(dict(row, seconds=seconds), prices, now)
            counts[result] += 1
            first[attempts[0] if attempts else 'PENDING'] += 1
            if result == 'WIN':
                stages[len(attempts)] += 1
        def rate(c):
            n = c['WIN'] + c['LOSS']
            return f"{100*c['WIN']/n:.1f}% ({n})" if n else '— (0)'
        lines.extend([f'M{seconds//60} · прогнозов {len(subset)}',
                      f'WR цикла: {rate(counts)} · первая: {rate(first)}',
                      f"✅ {counts['WIN']} / ❌ {counts['LOSS']} · ① {stages[1]} ② {stages[2]} ③ {stages[3]}",
                      f"⏳ {counts['PENDING']} · ничья {counts['DRAW']} · нет данных {counts['UNKNOWN']}", ''])
    lines.append('WR исключает ничьи и нет данных. Источник исходов: котировки текущего потока.')
    return '\n'.join(lines)
