"""Causal market-regime and candle/location safeguards for research signals.

Only fully closed bars. Thresholds are hypotheses, not calibrated probabilities.
All decisions and rejection reasons are persisted for later ablation.
"""
import math

VERSION = "risk-policy-v1"
LOOKBACK = 20


def evaluate(bars, features, direction, state):
    result = {"version": VERSION, "regime": state.get("state", "unknown"),
              "strategy": "none", "gates": {}, "reason": None}
    if direction not in ("UP", "DOWN") or len(bars) < LOOKBACK + 1:
        result["reason"] = "insufficient_price_context"
        return result
    last = bars[-1]
    previous = bars[-LOOKBACK-1:-1]
    if any(not math.isfinite(v) for v in (last.open, last.high, last.low, last.close)):
        result["reason"] = "invalid_candle"
        return result
    span = last.high - last.low
    atr = features.get("atr14", 0) * last.close
    if span <= 0 or not math.isfinite(atr) or atr <= 0:
        result["reason"] = "invalid_volatility"
        return result
    body = abs(last.close - last.open) / span
    top_wick = (last.high - max(last.open, last.close)) / span
    bottom_wick = (min(last.open, last.close) - last.low) / span
    upper = max(b.high for b in previous)
    lower = min(b.low for b in previous)
    state_name = state.get("state", "unknown")
    result["metrics"] = {
        "body_fraction": round(body, 4),
        "upper_wick_fraction": round(top_wick, 4),
        "lower_wick_fraction": round(bottom_wick, 4),
        "distance_to_resistance_atr": round((upper - last.close) / atr, 4),
        "distance_to_support_atr": round((last.close - lower) / atr, 4),
        "atr_price": atr,
    }
    # Reject indecision, exhaustion, abrupt volatility and reversals through
    # prior swing levels before checking any strategy-specific entry.
    gates = result["gates"]
    gates["not_doji"] = body >= 0.15
    gates["no_excessive_volatility"] = not state.get("volatility_spike", False)
    gates["no_opposing_wick"] = (
        top_wick < 0.55 if direction == "UP" else bottom_wick < 0.55
    )
    # Close back within the prior range after piercing an extreme.
    gates["no_false_breakout"] = not (
        last.high > upper and last.close <= upper if direction == "UP"
        else last.low < lower and last.close >= lower
    )
    if state_name in ("uptrend", "downtrend"):
        result["strategy"] = "trend_continuation"
        gates["trend_aligned"] = (
            (state_name == "uptrend") == (direction == "UP")
        )
        gates["not_into_level"] = (
            upper - last.close > 0.25 * atr if direction == "UP"
            else last.close - lower > 0.25 * atr
        )
    elif state_name == "range":
        result["strategy"] = "range_rejection"
        if direction == "UP":
            gates["at_range_edge"] = last.low <= lower + 0.5 * atr
            gates["rejection_candle"] = bottom_wick >= 0.35 and bottom_wick > top_wick
            gates["oscillator_support"] = features.get("rsi14", 0.5) <= 0.55
        else:
            gates["at_range_edge"] = last.high >= upper - 0.5 * atr
            gates["rejection_candle"] = top_wick >= 0.35 and top_wick > bottom_wick
            gates["oscillator_support"] = features.get("rsi14", 0.5) >= 0.45
    else:
        result["reason"] = "market_transition_or_unknown"
        return result
    result["reason"] = next(
        ("filter_" + key for key, passed in gates.items() if not passed), "eligible"
    )
    return result
