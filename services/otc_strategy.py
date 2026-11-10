"""Conservative OTC candle filters. These are heuristics, not probability estimates."""

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Candle:
    time: int  # UTC minute start
    open: float
    high: float
    low: float
    close: float


def normalize_candles(raw: Iterable[dict], now: int) -> list[Candle]:
    """Accept only UTC seconds/milliseconds; never guess venue offsets."""
    import math

    from services.otc_market import utc_timestamp

    candles = {}
    conflicts = set()
    for item in raw:
        try:
            stamp = utc_timestamp(item["time"])
            start = int(stamp)
            values = [float(item[key]) for key in ("open", "high", "low", "close")]
            if (
                stamp != start
                or start % 60
                or start + 60 > now
                or not all(math.isfinite(v) and v > 0 for v in values)
                or values[1] < max(values[0], values[2], values[3])
                or values[2] > min(values[0], values[1], values[3])
            ):
                continue
            bar = Candle(start, *values)
            if start in candles and candles[start] != bar:
                conflicts.add(start)
            candles[start] = bar
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return [candles[k] for k in sorted(candles) if k not in conflicts]


def aggregate(candles: list[Candle], period: int) -> list[Candle]:
    """Only complete, uninterrupted groups of minute bars are emitted."""
    if period not in (60, 300, 900):
        raise ValueError("Unsupported timeframe")
    if period == 60:
        return list(candles)
    count = period // 60
    buckets: dict[int, list[Candle]] = {}
    for candle in candles:
        start = candle.time // period * period
        buckets.setdefault(start, []).append(candle)
    result = []
    for start, group in sorted(buckets.items()):
        group.sort(key=lambda c: c.time)
        if len(group) != count or any(
            c.time != start + i * 60 for i, c in enumerate(group)
        ):
            continue
        result.append(
            Candle(
                start,
                group[0].open,
                max(c.high for c in group),
                min(c.low for c in group),
                group[-1].close,
            )
        )
    return result


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    value = sum(values[:period]) / period
    weight = 2 / (period + 1)
    for price in values[period:]:
        value = price * weight + value * (1 - weight)
    return value


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains = [max(0, values[i] - values[i - 1]) for i in range(1, len(values))]
    losses = [max(0, values[i - 1] - values[i]) for i in range(1, len(values))]
    gain = sum(gains[:period]) / period
    loss = sum(losses[:period]) / period
    for up, down in zip(gains[period:], losses[period:]):
        gain = (gain * (period - 1) + up) / period
        loss = (loss * (period - 1) + down) / period
    if loss == 0:
        return 100.0 if gain else 50.0
    return 100 - 100 / (1 + gain / loss)


def atr(candles: list[Candle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    recent = candles[-period - 1 :]
    ranges = [
        max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))
        for prev, c in zip(recent, recent[1:])
    ]
    return sum(ranges) / period


def _continuous(candles: list[Candle], period: int, count: int) -> bool:
    if len(candles) < count:
        return False
    sample = candles[-count:]
    return all(b.time - a.time == period for a, b in zip(sample, sample[1:]))


def candidate(
    minutes: list[Candle], seconds: int, closed_at: int
) -> tuple[str, str] | None:
    """Signal only at the close of a *complete* candle, using no future data."""
    if seconds not in (60, 300, 900):
        return None
    bars = aggregate([c for c in minutes if c.time + 60 <= closed_at], seconds)
    if not bars or bars[-1].time + seconds != closed_at:
        return None
    if not _continuous(bars, seconds, 55):
        return None
    closes = [bar.close for bar in bars]
    fast = ema(closes, 20)
    slow = ema(closes, 50)
    previous_fast = ema(closes[:-3], 20)
    strength = atr(bars)
    oscillator = rsi(closes)
    if None in (fast, slow, previous_fast, strength, oscillator) or strength <= 0:
        return None
    last = bars[-1]
    candle_range = last.high - last.low
    if candle_range <= 0 or abs(last.close - last.open) / candle_range < 0.5:
        return None
    # Exclude extraordinary spikes and very flat EMA slopes.
    if candle_range > 2.5 * strength or abs(fast - previous_fast) < 0.08 * strength:
        return None
    bullish = (
        fast > slow
        and last.close > fast
        and 53 <= oscillator <= 68
        and last.close > last.open
    )
    bearish = (
        fast < slow
        and last.close < fast
        and 32 <= oscillator <= 47
        and last.close < last.open
    )
    if not bullish and not bearish:
        return None
    direction = "UP" if bullish else "DOWN"
    # Compare with next higher timeframe using ONLY already closed bars.
    higher = 300 if seconds == 60 else 900 if seconds == 300 else 900
    if higher != seconds:
        higher_bars = [
            c for c in aggregate(minutes, higher) if c.time + higher <= closed_at
        ]
        if not _continuous(higher_bars, higher, 50):
            return None
        upper_closes = [c.close for c in higher_bars]
        upper_fast = ema(upper_closes, 20)
        upper_slow = ema(upper_closes, 50)
        if direction == "UP" and not upper_fast > upper_slow:
            return None
        if direction == "DOWN" and not upper_fast < upper_slow:
            return None
    return direction, f"EMA20/50, RSI {oscillator:.1f}, ATR и свеча; фильтр старшего ТФ"
