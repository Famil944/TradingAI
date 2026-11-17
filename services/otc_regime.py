"""Fixed descriptive market-state hypotheses, never an entry gate or extra vote."""

import math
from statistics import median

VERSION = "closed-regime-v1"
CONTEXT_VERSION = "closed-context-v1"


def context(bars, period, asof):
    """Describe completed candles only. Fixed hypotheses, no prediction or vote."""
    result = dict(version=CONTEXT_VERSION, available=False, heuristic=True)
    closed = [b for b in bars if b.time + period <= asof][-21:]
    if (
        len(closed) < 21
        or not 0 <= asof - closed[-1].time - period < period
        or any(b.time - a.time != period for a, b in zip(closed, closed[1:]))
    ):
        return result
    if any(
        not all(math.isfinite(v) and v > 0 for v in (b.open, b.high, b.low, b.close))
        or not b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
        for b in closed
    ):
        return result
    previous, last = closed[:-1], closed[-1]
    spans = [b.high - b.low for b in previous]
    baseline = median(spans)
    span = last.high - last.low
    upper, lower = max(b.high for b in previous), min(b.low for b in previous)
    if baseline <= 0 or upper <= lower:
        return result
    ratio = span / baseline
    volatility = "сжатие" if ratio <= 0.6 else "высокая" if ratio >= 1.8 else "обычная"
    position = (last.close - lower) / (upper - lower)
    location = (
        "у нижней границы"
        if position <= 0.2
        else "у верхней границы"
        if position >= 0.8
        else "в середине диапазона"
    )
    # These are observed exits/returns, not claims of a confirmed future reversal.
    event = "без пробоя"
    if last.close > upper:
        event = "выход вверх из диапазона"
    elif last.close < lower:
        event = "выход вниз из диапазона"
    elif last.high > upper and last.low < lower:
        event = "выход за обе границы и возврат"
    elif last.high > upper:
        event = "выход вверх и возврат"
    elif last.low < lower:
        event = "выход вниз и возврат"
    body = abs(last.close - last.open) / span if span else 0
    upper_wick = (last.high - max(last.open, last.close)) / span if span else 0
    lower_wick = (min(last.open, last.close) - last.low) / span if span else 0
    candle = (
        "доджи / малое тело"
        if body <= 0.15
        else "сильное тело"
        if body >= 0.7
        else "обычное тело"
    )
    colors = []
    for b in closed[-6:]:
        size = b.high - b.low
        colors.append(
            0
            if size == 0 or abs(b.close - b.open) <= 0.15 * size
            else 1
            if b.close > b.open
            else -1
        )
    flips = sum(a * b == -1 for a, b in zip(colors, colors[1:]))
    return dict(
        result,
        available=True,
        period=period,
        asof=asof,
        last_closed_at=last.time + period,
        volatility=volatility,
        range_ratio=ratio,
        range_position=position,
        location=location,
        event=event,
        candle=candle,
        body_fraction=body,
        upper_wick_fraction=upper_wick,
        lower_wick_fraction=lower_wick,
        color_changes=flips,
        color_transitions=5,
        alternating=flips >= 4,
    )


def describe_context(value):
    if not value or not value.get("available"):
        return ""
    parts = [value["location"], "волатильность: " + value["volatility"]]
    if value["event"] != "без пробоя":
        parts.append(value["event"])
    if value["alternating"]:
        parts.append("частая смена цвета")
    if value["candle"] != "обычное тело":
        parts.append(value["candle"])
    if value["upper_wick_fraction"] >= 0.55:
        parts.append("длинная верхняя тень")
    if value["lower_wick_fraction"] >= 0.55:
        parts.append("длинная нижняя тень")
    return " · ".join(parts)


def classify(features):
    keys = ("adx14", "atr14", "ema_spread", "return1", "range_atr")
    if any(
        not isinstance(features.get(k), (int, float)) or not math.isfinite(features[k])
        for k in keys
    ):
        return dict(
            version=VERSION,
            state="unknown",
            label="недостаточно данных",
            pullback=False,
            volatility_spike=False,
        )
    adx, atr, spread = (features[k] for k in keys[:3])
    if atr <= 0 or adx < 0.18:
        state, label = "range", "флэт"
    elif adx >= 0.25 and abs(spread) >= 0.1 * atr:
        state, label = (
            ("uptrend", "восходящий тренд")
            if spread > 0
            else ("downtrend", "нисходящий тренд")
        )
    else:
        state, label = "transition", "неопределённый / переходный"
    pullback = state in ("uptrend", "downtrend") and features["return1"] * spread < 0
    spike = features["range_atr"] >= 2.5
    return dict(
        version=VERSION,
        state=state,
        label=label,
        pullback=pullback,
        volatility_spike=spike,
        heuristic=True,
    )


def describe(state):
    parts = [state["label"]]
    if state.get("pullback"):
        parts.append("возможный откат")
    if state.get("volatility_spike"):
        parts.append("всплеск волатильности")
    return " · ".join(parts)
