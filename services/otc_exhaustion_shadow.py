"""Fixed prospective exhaustion/rejection hypothesis. No live delivery."""
import asyncio
import math
import time
from pathlib import Path
from statistics import median

from services.otc_rsi_shadow import RSIShadow, candidates
from services.otc_cycle_dashboard import cycle_block
from services.otc_strategies_live import evaluate_cycle
from services.otc_validation_report import load

VERSION = 'exhaustion-rejection-m1-v1'


def decision(minutes, asof):
    bars = [b for b in minutes if b.time + 60 <= asof][-30:]
    if len(bars) < 30 or bars[-1].time + 60 != asof:
        return None
    if any(b.time-a.time != 60 for a,b in zip(bars,bars[1:])):
        return None
    if any(not all(math.isfinite(v) and v > 0 for v in (b.open,b.high,b.low,b.close))
           or not b.low <= min(b.open,b.close) <= max(b.open,b.close) <= b.high for b in bars):
        return None
    # Baseline excludes the impulse and confirming candle.
    base = bars[-25:-4]
    ranges = [max(b.high-b.low, abs(b.high-a.close), abs(b.low-a.close))
              for a,b in zip(base,base[1:])]
    scale = median(ranges)
    if scale <= 0:
        return None
    path = sum(abs(b.close-a.close) for a,b in zip(base,base[1:]))
    efficiency = abs(base[-1].close-base[0].close)/path if path else 0
    if efficiency > 0.75:
        return None  # Avoid fading a persistent directional market.
    impulse = bars[-2].close-bars[-5].close
    if not 1.5 <= abs(impulse)/scale <= 4:
        return None
    last, previous = bars[-1], bars[-2]
    span = last.high-last.low
    if not 0.5*scale <= span <= 2.5*scale:
        return None
    direction = None
    if (impulse < 0 and last.low <= previous.low and last.close > last.open
            and last.close > previous.close
            and (last.close-last.low)/span >= 0.65
            and (min(last.open,last.close)-last.low)/span >= 0.35):
        direction = 'UP'
    elif (impulse > 0 and last.high >= previous.high and last.close < last.open
            and last.close < previous.close
            and (last.high-last.close)/span >= 0.65
            and (last.high-max(last.open,last.close))/span >= 0.35):
        direction = 'DOWN'
    if direction is None:
        return None
    return dict(direction=direction, baseline_range=scale, impulse_normalized=impulse/scale,
                efficiency=efficiency, rejection_range=span, last_closed=last.time,
                analysis_period=60, rule_version=VERSION)


class ExhaustionShadow(RSIShadow):
    filename = 'exhaustion-shadow.db'
    label = 'Exhaustion-v1'

    def candidate_rows(self, snapshot, now):
        return candidates(snapshot, now, decision_fn=decision, version=VERSION)


class ShadowObservers:
    """Share one quote stream; each experiment keeps separate storage and slots."""
    def __init__(self, *observers):
        self.observers = observers

    @property
    def snapshot(self):
        return self.observers[0].snapshot

    @snapshot.setter
    def snapshot(self, value):
        for observer in self.observers:
            observer.snapshot = value

    def observe(self, *args):
        for observer in self.observers:
            observer.observe(*args)

    def observe_tick(self, tick, now):
        for observer in self.observers:
            observer.observe_tick(tick, now)

    async def run(self, session, cycles):
        tasks = [asyncio.create_task(o.run(session,cycles)) for o in self.observers]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def report(root, now=None):
    now = time.time() if now is None else now
    rows, prices = load(Path(root)/'data'/ExhaustionShadow.filename,0,now)
    outcomes = []
    for row in rows:
        if row.get('version') != VERSION or row.get('cycle_seconds') != 120:
            continue
        attempts, _, state = evaluate_cycle(dict(row,seconds=120),prices,now)
        outcomes.append(dict(outcome=state,attempts=attempts))
    text = '🧪 Свой метод · Истощение и отскок\nM2 · версия 1 · с начала теста\n\n'
    text += cycle_block('Главный показатель — полный цикл ×3',outcomes,detailed=True)
    text += ('\n\nWR = WIN / (WIN + LOSS). Ничьи и нет данных исключены.'
             '\nПовтор только после LOSS. При WIN, ничьей или ❔ цикл остановлен.'
             '\nЭто фоновая проверка гипотезы, не сделки и не подтверждённая точность.')
    return text
