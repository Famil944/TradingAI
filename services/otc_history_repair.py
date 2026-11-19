"""Lossless, conservative reconciliation of observed closed OTC minute candles.

This is a transport/history utility, NOT a forecasting rule. Every retained bar
comes from a previously observed closed candle or from a provider response.
No missing minute is synthesized; the unchanged downstream continuity guard
is responsible for rejecting gaps.
"""

import math


def _valid_closed(bar, now):
    at = getattr(bar, "time", None)
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return False
    if int(at) != at or int(at) % 60 or int(at) + 60 > now:
        return False
    values = [getattr(bar, name, None) for name in ("open", "high", "low", "close")]
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(value) or value <= 0 for value in values
    ):
        return False
    op, hi, lo, cl = values
    return lo <= op <= hi and lo <= cl <= hi


def reconcile_closed_history(previous, canonical, now, limit=1200):
    """Prefer newly fetched canonical OHLC at equal timestamps, retain other
    genuine closed observations, and discard future/invalid bars.

    A partial API response cannot erase otherwise valid observed candles.
    This function deliberately does not fill holes or grant signal readiness.
    """
    combined = {}
    for bar in previous:
        if _valid_closed(bar, now):
            combined[int(bar.time)] = bar
    for bar in canonical:
        if _valid_closed(bar, now):
            combined[int(bar.time)] = bar
    return [combined[t] for t in sorted(combined)[-limit:]]


def contiguous_tail_minutes(bars):
    """Diagnostic only. Number of consecutive 60-second bars at the end."""
    if not bars:
        return 0
    count = 1
    for older, newer in zip(reversed(bars[:-1]), reversed(bars[1:])):
        if int(newer.time) - int(older.time) != 60:
            break
        count += 1
    return count
