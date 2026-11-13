"""Strict M1 candidate screening with a strictly *closed* M5 context.

This is a testable, deterministic hypothesis, not a profitability claim.
All clocks are UTC. Never use the unfinished M1/M5 candle.
"""
import math

from services.otc_strategy import aggregate, atr, ema

VERSION = "m1-closed-m5-v1"
M5_COUNT = 26


def live_check(reference_close, atr_price, direction, tick, now):
    """Check real observed tick freshness and adverse/extended pre-entry movement."""
    answer = dict(passed=False, reason="m1_missing_fresh_tick")
    if (
        tick is None or len(tick) != 3 or direction not in ("UP", "DOWN")
        or not isinstance(reference_close, (int, float))
        or not isinstance(atr_price, (int, float))
        or not all(math.isfinite(x) for x in (reference_close, atr_price, now))
        or reference_close <= 0 or atr_price <= 0
    ):
        return answer
    price, tick_at, received_at = tick
    if (
        not all(isinstance(x, (int, float)) and math.isfinite(x)
                for x in (price, tick_at, received_at))
        or price <= 0
        or not 0 <= received_at - tick_at <= 2.0
        or not 0 <= now - tick_at <= 3.0
        or not 0 <= now - received_at <= 3.0
    ):
        answer["reason"] = "m1_stale_tick"
        return answer
    move = (price - reference_close) / atr_price
    directional_move = move if direction == "UP" else -move
    answer["move_atr"] = round(move, 4)
    answer["directional_move_atr"] = round(directional_move, 4)
    answer["tick_age_s"] = round(now - tick_at, 3)
    if directional_move < -0.25:
        answer["reason"] = "m1_adverse_live_move"
    elif abs(move) > 0.70 or directional_move > 0.55:
        answer["reason"] = "m1_preentry_overextension"
    else:
        answer.update(passed=True, reason="eligible")
    return answer


def evaluate(minutes, features, direction, state, asof, tick, now):
    """Return reason/gates for M1, plus reference prices for a final tick recheck."""
    result = dict(version=VERSION, passed=False, reason="m1_insufficient_history",
                  gates={}, metrics={})
    if direction not in ("UP", "DOWN") or not isinstance(asof, int):
        return result
    past = [b for b in minutes if b.time + 60 <= asof]
    if len(past) < 160 or past[-1].time + 60 != asof:
        return result
    # Protect against gaps and backfilled/partially closed M5 candles.
    tail = past[-160:]
    if any(b.time - a.time != 60 for a, b in zip(tail, tail[1:])):
        result["reason"] = "m1_gapped_history"
        return result
    higher = aggregate(past, 300)
    if len(higher) < M5_COUNT:
        result["reason"] = "m1_insufficient_closed_m5"
        return result
    last_m5 = higher[-M5_COUNT:]
    if (
        any(b.time - a.time != 300 for a, b in zip(last_m5, last_m5[1:]))
        or not 0 <= asof - (last_m5[-1].time + 300) < 300
    ):
        result["reason"] = "m1_stale_closed_m5"
        return result
    prices = [bar.close for bar in last_m5]
    fast = ema(prices, 8)
    slow = ema(prices, 21)
    previous_fast = ema(prices[:-1], 8)
    higher_atr = atr(last_m5)
    last = past[-1]
    atr_price = features.get("atr14", 0) * last.close
    if any(
        x is None or not isinstance(x, (int, float)) or not math.isfinite(x)
        for x in (fast, slow, previous_fast, higher_atr, atr_price)
    ) or higher_atr <= 0 or atr_price <= 0:
        result["reason"] = "m1_invalid_volatility"
        return result
    result["metrics"] = dict(
        m5_closed_at=last_m5[-1].time + 300, m5_fast=round(fast, 8),
        m5_slow=round(slow, 8), m5_atr=round(higher_atr, 8),
        m1_reference_close=last.close, m1_atr_price=atr_price,
        m1_last_move_atr=round(abs(last.close - past[-2].close) / atr_price, 4),
    )
    state_name = state.get("state", "unknown")
    gates = result["gates"]
    aligned = (
        (fast > slow and previous_fast < fast and prices[-1] > slow)
        if direction == "UP"
        else (fast < slow and previous_fast > fast and prices[-1] < slow)
    )
    gates["closed_m5_trend"] = aligned and abs(fast - slow) >= 0.10 * higher_atr
    gates["m1_trend"] = (
        state_name == ("uptrend" if direction == "UP" else "downtrend")
    )
    gates["no_last_candle_impulse"] = (
        abs(last.close - past[-2].close) <= 1.25 * atr_price
        and last.high - last.low <= 1.80 * atr_price
    )
    check = live_check(last.close, atr_price, direction, tick, now)
    result["live"] = check
    gates["fresh_tick"] = check["passed"]
    result["reason"] = next(
        ("m1_filter_" + key for key, passed in gates.items() if not passed),
        "eligible",
    )
    if not gates["fresh_tick"] and all(v for k, v in gates.items() if k != "fresh_tick"):
        result["reason"] = check["reason"]
    result["passed"] = result["reason"] == "eligible"
    return result
