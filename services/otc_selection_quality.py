"""Fixed prospective selection rules, not claims of calibrated probabilities."""

import math


def confirmation_families(confirmations):
    checks = confirmations.get("checks") or {}
    return sum(
        (
            checks.get("ema") == "agree" or checks.get("macd") == "agree",
            checks.get("rsi") == "agree",
            checks.get("candle") == "agree",
            checks.get("alignment") == "agree",
        )
    )


def not_extended(features, context, direction):
    values = [features.get(k) for k in ("atr14", "range_atr", "ema20_gap")]
    position = context.get("range_position")
    if (
        not context.get("available")
        or not all(
            isinstance(v, (float, int)) and math.isfinite(v)
            for v in values + [position]
        )
        or values[0] <= 0
    ):
        return None
    atr, candle_range, ema_gap = values
    return bool(
        candle_range <= 1.8
        and abs(ema_gap) / atr <= 1.5
        and (0 <= position <= 0.8 if direction == "UP" else 0.2 <= position <= 1)
    )


def primary_rejection(evidence, seconds, direction):
    confirmations = evidence.get("confirmations") or {}
    if confirmation_families(confirmations) < 2:
        return "fewer_than_two_confirmation_families"
    if (
        seconds == 300
        and not_extended(
            evidence.get("features") or {},
            confirmations.get("market_context") or {},
            direction,
        )
        is not True
    ):
        return "m5_extended_or_missing_context"
    return None
