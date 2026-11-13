"""Fixed descriptive market-state hypotheses, never an entry gate or extra vote."""

import math

VERSION = "closed-regime-v1"


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
