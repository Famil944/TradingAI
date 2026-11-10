"""Deterministic causal indicators. Input contains fully closed bars only."""

import math
from statistics import fmean, pstdev

from services.otc_strategy import ema, rsi

FEATURE_NAMES = (
    "ema20_gap",
    "ema50_gap",
    "ema_spread",
    "rsi14",
    "macd",
    "macd_signal",
    "macd_hist",
    "adx14",
    "atr14",
    "bb_width",
    "bb_position",
    "return1",
    "return5",
    "return15",
    "body_atr",
    "range_atr",
)
FEATURE_VERSION = "closed-ohlc-ema-rsi-macd-adx-atr-bb-v1"


def ema_series(values, period):
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def wilder(values, period):
    if len(values) < period:
        return []
    result = [fmean(values[:period])]
    for value in values[period:]:
        result.append(((period - 1) * result[-1] + value) / period)
    return result


def indicators(bars):
    """Call only with a chronological prefix of complete equally spaced bars."""
    if len(bars) < 60:
        return None
    period = bars[-1].time - bars[-2].time
    if period <= 0 or any(b.time - a.time != period for a, b in zip(bars, bars[1:])):
        return None
    closes = [b.close for b in bars]
    if not all(
        math.isfinite(v) and v > 0
        for b in bars
        for v in (b.open, b.high, b.low, b.close)
    ):
        return None
    close = closes[-1]
    fast, slow = ema(closes, 20), ema(closes, 50)
    macd = [a - b for a, b in zip(ema_series(closes, 12), ema_series(closes, 26))]
    signal = ema_series(macd, 9)[-1]
    ranges = []
    plus = []
    minus = []
    for previous, current in zip(bars, bars[1:]):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
        up = current.high - previous.high
        down = previous.low - current.low
        plus.append(up if up > down and up > 0 else 0)
        minus.append(down if down > up and down > 0 else 0)
    smoothed_tr = wilder(ranges, 14)
    dx = []
    for tr, p, m in zip(smoothed_tr, wilder(plus, 14), wilder(minus, 14)):
        pdi = 100 * p / tr if tr else 0
        mdi = 100 * m / tr if tr else 0
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0)
    adx = wilder(dx, 14)[-1]
    atr = smoothed_tr[-1]
    middle = fmean(closes[-20:])
    deviation = pstdev(closes[-20:])
    width = 4 * deviation
    denominator = max(atr, close * 1e-12)
    values = [
        (close - fast) / close,
        (close - slow) / close,
        (fast - slow) / close,
        rsi(closes) / 100,
        macd[-1] / close,
        signal / close,
        (macd[-1] - signal) / close,
        adx / 100,
        atr / close,
        width / middle,
        (close - (middle - 2 * deviation)) / width if width else 0.5,
        close / closes[-2] - 1,
        close / closes[-6] - 1,
        close / closes[-16] - 1,
        (bars[-1].close - bars[-1].open) / denominator,
        (bars[-1].high - bars[-1].low) / denominator,
    ]
    return tuple(values) if all(math.isfinite(v) for v in values) else None
