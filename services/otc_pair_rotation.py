"""Conservative, bounded OTC stream-universe rotation.

Only live-observed quote/candle health, the current catalogue, and delivery
timestamps influence selection. Historical WIN/LOSS and hypothetical signal
returns never rank instruments. The caller must verify the pending journals
again before closing a stream and must prefetch valid history for the new pair.
"""

import math
import sqlite3
from contextlib import closing
from pathlib import Path


MIN_TENURE = 180
FAIR_TENURE = 600
NO_TICK_SECONDS = 120
NO_CANDLE_SECONDS = 300
RECENT_SIGNAL_SECONDS = 600
RETURN_COOLDOWN = 1800
MAX_PAYOUT_DROP = 2
MIN_CANDLES = 600


def _payout(asset):
    value = getattr(asset, "payout", None)
    return (
        float(value) if isinstance(value, (int, float))
        and not isinstance(value, bool) and math.isfinite(value)
        and 90 <= value <= 100 else 0.0
    )


def journal_activity(paths, now):
    """Read pending forecasts and recently delivered signals without writes."""
    protected, recent = set(), set()
    for path, table in paths:
        path = Path(path)
        if not path.is_file() or table not in ("signals", "predictions"):
            return None
        try:
            with closing(sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2
            )) as db:
                for symbol, state, sent_at in db.execute(
                    f"SELECT symbol,outcome,sent_at FROM {table} "
                    "WHERE outcome='PENDING' OR sent_at>=?",
                    (now - RECENT_SIGNAL_SECONDS,),
                ):
                    if state == "PENDING":
                        protected.add(symbol)
                    if sent_at is not None and sent_at >= now - RECENT_SIGNAL_SECONDS:
                        recent.add(symbol)
        except sqlite3.OperationalError:
            # An unreadable journal must not authorize a disruptive rotation.
            return None
    return protected, recent


def choose_pair_swap(
    assets, chosen, now, selected_at, last_tick, last_candle,
    protected, recent, last_removed,
):
    """Return (victim, replacement, reason), or None; replace at most one pair."""
    if not chosen:
        return None
    allowed = {a.symbol: a for a in assets}
    alternatives = [
        a for a in assets
        if a.symbol not in chosen and _payout(a) >= 90
        and now - last_removed.get(a.symbol, float("-inf")) >= RETURN_COOLDOWN
    ]
    if not alternatives:
        return None

    available = [s for s in chosen if s not in protected]
    if not available:
        return None
    # First, recover unavailable/stale instruments, without waiting for a
    # nonexistent signal. A missing quote is evaluated only after warm-up.
    for symbol in available:
        if symbol not in allowed:
            return symbol, alternatives[0], "catalogue_ineligible"
    for symbol in available:
        age = now - selected_at.get(symbol, now)
        if age < MIN_TENURE:
            continue
        if now - last_tick.get(symbol, float("-inf")) >= NO_TICK_SECONDS:
            return symbol, alternatives[0], "no_fresh_ticks"
        if age >= NO_CANDLE_SECONDS and (
            now - last_candle.get(symbol, float("-inf")) >= NO_CANDLE_SECONDS
        ):
            return symbol, alternatives[0], "no_complete_candles"

    # Prefer a meaningfully better offered payout, with a dwell period so
    # minor catalogue fluctuations cannot cause reconnect storms.
    for symbol in sorted(available, key=lambda s: (_payout(allowed[s]), s)):
        if now - selected_at.get(symbol, now) < 600:
            continue
        better = next(
            (a for a in alternatives if _payout(a) >= _payout(allowed[symbol]) + 2),
            None,
        )
        if better:
            return symbol, better, "better_payout"

    # Slowly explore equally attractive pairs only when a slot has not
    # delivered any signal recently. Absence of a signal is not a LOSS.
    for symbol in sorted(
        available,
        key=lambda s: (_payout(allowed[s]), selected_at.get(s, now), s),
    ):
        if (
            now - selected_at.get(symbol, now) < FAIR_TENURE
            or symbol in recent
        ):
            continue
        comparable = next(
            (a for a in alternatives
             if _payout(allowed[symbol]) >= 90
             and _payout(a) >= _payout(allowed[symbol]) - MAX_PAYOUT_DROP),
            None,
        )
        if comparable:
            return symbol, comparable, "quiet_pair_exploration"
    return None


def usable_history(bars, now):
    """A new stream cannot trade from missing, old or disordered bars."""
    if len(bars) < MIN_CANDLES:
        return False
    tail = bars[-MIN_CANDLES:]
    if any(b.time + 60 > now for b in tail):
        return False
    if not 0 <= now - (tail[-1].time + 60) <= 120:
        return False
    if any(
        not all(
            isinstance(value, (int, float)) and math.isfinite(value) and value > 0
            for value in (bar.open, bar.high, bar.low, bar.close)
        ) or not (bar.low <= bar.open <= bar.high and
                  bar.low <= bar.close <= bar.high)
        for bar in tail
    ):
        return False
    return all(a.time + 60 == b.time for a, b in zip(tail, tail[1:]))
