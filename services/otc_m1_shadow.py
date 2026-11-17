"""Two independent M1 hypotheses, evaluated prospectively in shadow only.

No Telegram sends, trading orders, additional API requests or wallet access.
The feature cutoff is the last fully closed M1 bar, and outcomes require
actual entry/expiry ticks recorded *after* a candidate was registered.
"""

import math
from collections import Counter
from statistics import median

from services.otc_delivery_guard import check_data
from services.otc_strategy import atr, ema, rsi
from services.otc_validation_report import load, resolve

VERSION = "m1-levels-study-v1"
METHODS = {
    "flat": "ФЛЭТ M1 · уровень / пробой",
    "correction": "КОРРЕКЦИЯ M1 · тренд / откат / RSI",
}


def _closed(minutes, asof):
    """A continuous, valid window of 80 already-closed one-minute candles."""
    bars = [b for b in minutes if b.time + 60 <= asof][-80:]
    if (
        len(bars) != 80
        or bars[-1].time + 60 != asof
        or any(b.time - a.time != 60 for a, b in zip(bars, bars[1:]))
        or any(
            not all(math.isfinite(v) and v > 0 for v in (b.open, b.high, b.low, b.close))
            or not b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
            for b in bars
        )
    ):
        return []
    return bars


def flat_signal(bars):
    """Rebound from a tested range edge or a closed-candle breakout."""
    previous, last = bars[-31:-1], bars[-1]
    volatility = atr(bars)
    oscillator = rsi([b.close for b in bars])
    if not volatility or volatility <= 0 or oscillator is None:
        return None
    # Median of extremes avoids one anomalous wick defining the range.
    support = median(sorted(b.low for b in previous)[:3])
    resistance = median(sorted((b.high for b in previous), reverse=True)[:3])
    width = resistance - support
    if not 2 * volatility <= width <= 12 * volatility:
        return None
    closes = [b.close for b in previous]
    short, long = ema(closes, 8), ema(closes, 21)
    if (
        abs(short - long) > 0.8 * volatility
        or abs(closes[-1] - closes[0]) > 0.55 * width
    ):
        return None
    touch_width = 0.35 * volatility
    if (
        sum(b.low <= support + touch_width for b in previous) < 2
        or sum(b.high >= resistance - touch_width for b in previous) < 2
    ):
        return None
    candle_width = last.high - last.low
    body = abs(last.close - last.open)
    margin = max(0.2 * volatility, 0.04 * width)
    evidence = {
        "support": support, "resistance": resistance,
        "atr": volatility, "rsi14": oscillator,
        "oversold": oscillator <= 30, "overbought": oscillator >= 70,
    }
    if candle_width > 0 and body >= 0.55 * candle_width:
        if last.open <= resistance + margin < last.close and oscillator >= 55:
            return dict(direction="UP", setup="breakout_up", **evidence)
        if last.open >= support - margin > last.close and oscillator <= 45:
            return dict(direction="DOWN", setup="breakout_down", **evidence)
    # In a range, wait for a completed reaction, not just an intrabar wick.
    bottom = (
        last.low <= support + touch_width
        and last.close >= support + touch_width
        and last.close > last.open
        and oscillator <= 47
    )
    top = (
        last.high >= resistance - touch_width
        and last.close <= resistance - touch_width
        and last.close < last.open
        and oscillator >= 53
    )
    if bottom != top:
        return dict(
            direction="UP" if bottom else "DOWN",
            setup="support_rebound" if bottom else "resistance_rebound",
            **evidence,
        )
    return None


def correction_signal(bars):
    """Countertrend pullback to EMA21, then confirmation with RSI turning."""
    closes = [b.close for b in bars]
    volatility = atr(bars)
    if not volatility or volatility <= 0:
        return None
    # Establish trend *before* the five-bar correction; avoid classifying a
    # bounce itself as evidence of a pre-existing trend.
    base = closes[:-6]
    fast, middle, slow = ema(base, 9), ema(base, 21), ema(base, 50)
    if None in (fast, middle, slow):
        return None
    up = fast > middle > slow and fast - slow > 0.6 * volatility
    down = fast < middle < slow and slow - fast > 0.6 * volatility
    if not (up or down):
        return None
    last, prior = bars[-1], bars[-2]
    oscillator, previous_rsi = rsi(closes), rsi(closes[:-1])
    if oscillator is None or previous_rsi is None:
        return None
    recent_rsi = [rsi(closes[:i]) for i in range(len(closes) - 5, len(closes))]
    if any(v is None for v in recent_rsi):
        return None
    ema_level = ema(closes[:-1], 21)
    if ema_level is None:
        return None
    recent = bars[-6:-1]
    margin = 0.4 * volatility
    evidence = {
        "ema21": ema_level, "atr": volatility,
        "rsi14": oscillator, "previous_rsi14": previous_rsi,
        "oversold": min(recent_rsi) <= 30,
        "overbought": max(recent_rsi) >= 70,
    }
    if (
        up
        and min(b.low for b in recent) <= ema_level + margin
        and last.close > ema_level
        and last.close > last.open
        and last.close > prior.close
        and 30 <= oscillator <= 65
        and previous_rsi <= 45
        and oscillator >= previous_rsi + 2
    ):
        return dict(direction="UP", setup="bullish_pullback", **evidence)
    if (
        down
        and max(b.high for b in recent) >= ema_level - margin
        and last.close < ema_level
        and last.close < last.open
        and last.close < prior.close
        and 35 <= oscillator <= 70
        and previous_rsi >= 55
        and oscillator <= previous_rsi - 2
    ):
        return dict(direction="DOWN", setup="bearish_pullback", **evidence)
    return None


def signals(minutes, asof):
    bars = _closed(minutes, asof)
    if not bars:
        return {}
    results = {}
    for method, fn in (("flat", flat_signal), ("correction", correction_signal)):
        decision = fn(bars)
        if decision is not None:
            results[method] = decision
    return results


def prepare(snapshot, now):
    """Create two distinct research candidates only before a future M1 entry."""
    history, assets, quotes, end = snapshot
    entry = int(now) // 60 * 60 + 60
    if not 15 <= entry - now <= 20:
        return []
    rows = []
    for symbol, minutes in history.items():
        asset = assets.get(symbol)
        if asset is None:
            continue
        candidate = dict(
            symbol=symbol,
            name=getattr(asset, "name", None) or symbol,
            entry_at=entry,
            expires_at=entry + 60,
            feature_asof=entry - 60,
            observed_at=now,
            gates={"fresh_data": True},
            selected=False,
        )
        if check_data(candidate, snapshot, now):
            continue
        for method, decision in signals(list(minutes), entry - 60).items():
            rows.append(dict(
                candidate,
                version=f"m1-{method}-shadow-v1",
                direction=decision["direction"],
                payout=asset.payout,
                evidence={"pattern": decision},
                experiment=dict(version=VERSION, method=method, shadow_only=True),
            ))
    return rows


def report(path, since, now):
    """Separate M1 outcomes; never count missing ticks as wins or losses."""
    rows, prices = load(path, since, now)
    counts = {method: Counter() for method in METHODS}
    for row in rows:
        experiment = row.get("experiment") or {}
        method = experiment.get("method")
        if experiment.get("version") != VERSION or method not in METHODS:
            continue
        counts[method][resolve(row, prices, now)] += 1
    return counts


def text_report(counts):
    lines = ["🧪 M1 · фоновое исследование", "Одна попытка · без рассылки и ставок"]
    for method, label in METHODS.items():
        c = counts.get(method, {})
        wins, losses = c.get("WIN", 0), c.get("LOSS", 0)
        rate = f"{100 * wins / (wins + losses):.1f}%" if wins + losses else "—"
        lines.append(
            f"\n{label}\n"
            f"WIN {wins} · LOSS {losses} · WR {rate}\n"
            f"Ничьи {c.get('DRAW', 0)} · ожидают {c.get('PENDING', 0)} · "
            f"нет данных {c.get('UNKNOWN', 0)}"
        )
    lines.append("\nРезультаты по тикам OTCharts, не подтверждение сделок Pocket Option.")
    return "\n".join(lines)
