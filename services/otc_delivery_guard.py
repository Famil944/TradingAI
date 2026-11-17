"""Atomic cross-source delivery claims; ambiguous send failures retain the claim."""

import math
import sqlite3
import time
from contextlib import closing


def check_data(selected, snapshot, now):
    """Final read-only check shared by all three senders; never invent missing bars."""
    if snapshot is None:
        return "missing_snapshot"
    history, assets, quotes, end = snapshot
    symbol = selected["symbol"]
    if end and selected["expires_at"] + 60 > end:
        return "session_ending"
    quote = quotes.get(symbol)
    if (
        not isinstance(quote, (int, float))
        or not math.isfinite(quote)
        or not 0 <= now - quote <= 8
    ):
        return "stale_quote"
    asset = assets.get(symbol)
    if (
        asset is None
        or asset.open is not True
        or asset.payout is None
        or not math.isfinite(asset.payout)
        or not 90 <= asset.payout <= 100
        or asset.observed_at is None
        or not 0 <= now - asset.observed_at <= 60
    ):
        return "stale_or_ineligible_asset"
    cutoff = selected.get("feature_asof", now)
    if (
        not isinstance(cutoff, (int, float))
        or not math.isfinite(cutoff)
        or cutoff > now
    ):
        return "invalid_feature_cutoff"
    bars = [b for b in history.get(symbol, []) if b.time + 60 <= cutoff][-60:]
    if len(bars) < 60:
        return "insufficient_closed_history"
    if not 0 <= now - bars[-1].time - 60 <= 65:
        return "stale_closed_history"
    if any(b.time - a.time != 60 for a, b in zip(bars, bars[1:])):
        return "history_gap"
    if any(
        not all(math.isfinite(v) and v > 0 for v in (b.open, b.high, b.low, b.close))
        or not b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
        for b in bars
    ):
        return "invalid_closed_history"
    return None


def claim(path, selected, source, now=None):
    with closing(sqlite3.connect(path, timeout=1)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS claims(
            symbol TEXT, direction TEXT, entry_at INTEGER, expires_at INTEGER,
            source TEXT, claimed_at REAL,
            PRIMARY KEY(symbol,direction,entry_at,expires_at))""")
        db.execute("BEGIN IMMEDIATE")
        if db.execute(
            "SELECT 1 FROM claims WHERE symbol=? AND entry_at=? LIMIT 1",
            (selected["symbol"], selected["entry_at"]),
        ).fetchone():
            return False
        cursor = db.execute(
            "INSERT OR IGNORE INTO claims VALUES(?,?,?,?,?,?)",
            (
                selected["symbol"],
                selected["direction"],
                selected["entry_at"],
                selected["expires_at"],
                source,
                time.time() if now is None else now,
            ),
        )
        db.commit()
        return cursor.rowcount == 1
