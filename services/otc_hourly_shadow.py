"""One-hour OTC candle experiment, five minutes before a NEW UTC H1 candle.

Strictly shadow-only: no Telegram delivery, new feeds, broker actions or
relaxation of the existing S1-S4 forecast functions. A single H1 candle is
evaluated (not a synthetic three-attempt / three-hour cycle).
"""
import math
import sqlite3
from contextlib import closing
from pathlib import Path

from services.otc_community_cycles import signals as community_signals
from services.otc_delivery_guard import check_data
from services.otc_shadow_variants import pattern_directions
from services.otc_strategy import Candle
from services.otc_strategies_live import (
    RECOVERY_GRACE, _canonical_outcome, _tick_outcome,
)

H1_SECONDS = 3600
H1_WINDOW = (285, 300)  # 12:55:00..12:55:15 for entry at 13:00 UTC
H1_VERSION = "h1-five-minute-shadow-v1"
SOURCE_NAMES = ("S1", "S2", "S3", "S4")
METHODS = {"bb_reentry": "S2", "break_retest": "S3", "ema_pullback": "S4"}


def hourly_entry(now):
    return (int(now) // H1_SECONDS + 1) * H1_SECONDS


def observed_hourly_minutes(path, symbol, entry, observed_at):
    """Read only already witnessed, unrevised one-minute observations.

    Archive observations AFTER the forecast time cannot influence that forecast.
    Missing minutes remain missing; the unchanged 20/60-H1 strategy checks
    refuse non-contiguous price histories. Does not call OTCharts.
    """
    path = Path(path)
    if not path.is_file():
        return {}
    # 72 hours, enough for 60 closed H1 candles plus a small buffer.
    after = entry - 73 * H1_SECONDS
    last_completed_hour = entry - H1_SECONDS
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=2)) as db:
        try:
            records = db.execute(
                "SELECT time,open,high,low,close,source FROM bars "
                "WHERE symbol=? AND time>=? AND time+60<=? "
                "AND first_observed<=? AND last_observed<=? "
                "AND revisions=0 AND source IN ('otcharts_history','otcharts_ticks') "
                "ORDER BY time DESC LIMIT 4800",
                (symbol, after, last_completed_hour, observed_at, observed_at),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
    result = {}
    for t, op, hi, lo, cl, source in reversed(records):
        vals = (op, hi, lo, cl)
        if (t % 60 == 0
                and all(isinstance(v, (int, float)) and
                        not isinstance(v, bool) and math.isfinite(v) and v > 0
                        for v in vals)
                and lo <= min(op, cl) <= max(op, cl) <= hi):
            result[t] = (Candle(t, op, hi, lo, cl), source)
    return result


def hourly_candidate(snapshot, now, archive_path):
    """Return at most one prospective H1 forecast per entry hour.

    Fixed priority S1>S2>S3>S4, then payout and symbol. Never rank by
    later results. M2/M5/M15 strategy calculations remain unchanged.
    """
    entry = hourly_entry(now)
    if not H1_WINDOW[0] <= entry - now <= H1_WINDOW[1] or snapshot is None:
        return None
    history, assets, quotes, end = snapshot
    feature_asof = int(now // 60) * 60
    choices = []
    for symbol, bars in history.items():
        asset = assets.get(symbol)
        if asset is None:
            continue
        base = dict(
            symbol=symbol, name=asset.name or symbol,
            entry_at=entry, expires_at=entry + H1_SECONDS,
            cycle_seconds=H1_SECONDS,
            feature_asof=feature_asof, observed_at=now,
            payout=asset.payout, gates={"cycle": True},
        )
        if check_data(base, snapshot, now) is not None:
            continue
        # Use the same M1 provider history, extended from local archive when
        # possible; the live in-memory tail contains no future minute closes.
        complete = {
            b.time: b for b in bars
            if b.time + 60 <= entry - H1_SECONDS and b.time % 60 == 0
        }
        archived = observed_hourly_minutes(archive_path, symbol, entry, now)
        for t, (bar, source) in archived.items():
            if t not in complete or source == "otcharts_history":
                complete[t] = bar
        minutes = [complete[t] for t in sorted(complete)][-4800:]
        # No fabricated 60-minute candles: the aggregation utility itself
        # requires 60 uninterrupted M1 candles for each H1 bar.
        first = pattern_directions(minutes, H1_SECONDS, feature_asof).get(
            "bollinger_reversal"
        )
        found = {"S1": first} if first else {}
        found.update({
            METHODS[key]: value for key, value
            in community_signals(minutes, H1_SECONDS, feature_asof).items()
            if key in METHODS
        })
        for source in SOURCE_NAMES:
            decision = found.get(source)
            if not decision or decision.get("direction") not in ("UP", "DOWN"):
                continue
            priority = (SOURCE_NAMES.index(source), -asset.payout, symbol)
            choices.append((priority, dict(
                base, version=H1_VERSION, direction=decision["direction"],
                experiment=dict(
                    version=H1_VERSION, strategy="H1",
                    source_strategy=source, cycle_seconds=H1_SECONDS,
                    attempts=1, lead_window=list(H1_WINDOW), shadow_only=True,
                ),
            )))
    return min(choices, key=lambda item: item[0])[1] if choices else None


def hourly_outcome(row, prices, now, archive_path):
    """One confirmed 60-minute move, same price comparators as live cycles."""
    entry = int(row["entry_at"])
    end = entry + H1_SECONDS
    if now < end:
        return "PENDING"
    outcome = _tick_outcome(
        prices, row["symbol"], entry, end, row["direction"],
    )
    if outcome is None:
        outcome = _canonical_outcome(
            archive_path, row["symbol"], entry, end, row["direction"],
        )
    if outcome in ("WIN", "LOSS", "DRAW"):
        return outcome
    return "PENDING" if now < end + RECOVERY_GRACE else "UNKNOWN"
