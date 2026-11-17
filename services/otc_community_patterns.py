"""Fixed, causal adaptations of community ideas; shadow forecasts only."""

import math
from statistics import mean, pstdev

from services.otc_ml.features import FEATURE_NAMES, ema_series, indicators
from services.otc_strategy import aggregate, rsi

LABELS = {
    "regime_router": "Выбор метода по состоянию рынка",
    "ema_pullback": "Откат EMA9/19",
    "break_retest": "Пробой и ретест",
    "bb_rsi_reentry": "Боллинджер + RSI · возврат внутрь",
}


def route(decisions, state):
    """Fixed routing, no fallback to a winning historical method."""
    name = {
        "uptrend": "ema_pullback",
        "downtrend": "ema_pullback",
        "range": "bb_rsi_reentry",
        "transition": "break_retest",
    }.get(state)
    decision = decisions.get(name)
    if decision is None:
        return None
    if state in ("uptrend", "downtrend") and decision["direction"] != (
        "UP" if state == "uptrend" else "DOWN"
    ):
        return None
    return dict(
        decision,
        routed_method=name,
        market_state=state,
        rule_version="regime-router-v1",
    )


def detect(bars):
    """Input already validated and closed. Return independent direction per rule."""
    values = indicators(bars)
    if values is None:
        return {}
    features = dict(zip(FEATURE_NAMES, values))
    prices = [b.close for b in bars]
    fast, slow = ema_series(prices, 9), ema_series(prices, 19)
    last = bars[-1]
    result = {}
    up = (
        fast[-1] > slow[-1]
        and all(a < b for a, b in zip(fast[-4:], fast[-3:]))
        and all(a < b for a, b in zip(slow[-4:], slow[-3:]))
    )
    down = (
        fast[-1] < slow[-1]
        and all(a > b for a, b in zip(fast[-4:], fast[-3:]))
        and all(a > b for a, b in zip(slow[-4:], slow[-3:]))
    )
    if (
        up
        and last.low <= fast[-1] < last.close
        and last.close > last.open
        and last.low > slow[-1]
    ):
        result["ema_pullback"] = "UP"
    elif (
        down
        and last.high >= fast[-1] > last.close
        and last.close < last.open
        and last.high < slow[-1]
    ):
        result["ema_pullback"] = "DOWN"

    # Freeze the 20-bar level before the breakout, then allow 1..3 bars for retest.
    retests = set()
    tolerance = 0.15 * features["atr14"] * last.close
    for distance in (1, 2, 3):
        index = len(bars) - 1 - distance
        prior = bars[index - 20 : index]
        high, low = max(b.high for b in prior), min(b.low for b in prior)
        breakout = bars[index]
        between = bars[index + 1 : -1]
        if (
            breakout.close > high
            and all(b.close > high for b in between)
            and abs(last.low - high) <= tolerance
            and last.close > high
            and last.close > last.open
        ):
            retests.add("UP")
        if (
            breakout.close < low
            and all(b.close < low for b in between)
            and abs(last.high - low) <= tolerance
            and last.close < low
            and last.close < last.open
        ):
            retests.add("DOWN")
    if len(retests) == 1:
        result["break_retest"] = retests.pop()

    # Range-only re-entry, with RSI crossing back from 30/70 on the same close.
    previous = prices[:-1]
    mid0, dev0 = mean(previous[-20:]), pstdev(previous[-20:])
    mid1, dev1 = mean(prices[-20:]), pstdev(prices[-20:])
    if features["adx14"] < 0.2 and dev0 > 0 and dev1 > 0:
        r0, r1 = rsi(previous), rsi(prices)
        if (
            previous[-1] < mid0 - 2 * dev0
            and mid1 - 2 * dev1 < prices[-1] < mid1 + 2 * dev1
            and r0 < 30 <= r1
            and last.close > last.open
        ):
            result["bb_rsi_reentry"] = "UP"
        elif (
            previous[-1] > mid0 + 2 * dev0
            and mid1 - 2 * dev1 < prices[-1] < mid1 + 2 * dev1
            and r0 > 70 >= r1
            and last.close < last.open
        ):
            result["bb_rsi_reentry"] = "DOWN"
    return result


def forecasts(minutes, seconds, asof):
    period = 60 if seconds == 120 else seconds
    bars = aggregate([b for b in minutes if b.time + 60 <= asof], period)[-250:]
    if len(bars) < 60 or not 0 <= asof - bars[-1].time - period < period:
        return {}
    if any(b.time - a.time != period for a, b in zip(bars, bars[1:])):
        return {}
    if any(
        not all(math.isfinite(v) and v > 0 for v in (b.open, b.high, b.low, b.close))
        or not b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
        for b in bars
    ):
        return {}
    return {
        name: dict(
            direction=direction,
            analysis_period=period,
            last_closed=bars[-1].time,
            rule_version=name + "-v1",
        )
        for name, direction in detect(bars).items()
    }
