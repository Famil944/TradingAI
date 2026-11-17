"""Atomic cross-source entry claims; ambiguous send failures retain the claim."""

import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path


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
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS claims(
            symbol TEXT, direction TEXT, entry_at INTEGER, expires_at INTEGER,
            source TEXT, claimed_at REAL,
            PRIMARY KEY(symbol,direction,entry_at,expires_at))""")
        # Existing installations already have claims without active_until.
        # Keep the original end for audit, but allow a confirmed early WIN,
        # DRAW or UNKNOWN to end a three-attempt cycle sooner.
        columns = {row[1] for row in db.execute("PRAGMA table_info(claims)")}
        if "active_until" not in columns:
            db.execute("ALTER TABLE claims ADD COLUMN active_until REAL")
        db.execute(
            "CREATE INDEX IF NOT EXISTS claims_symbol_entry "
            "ON claims(symbol,entry_at)"
        )
        if db.execute(
            "SELECT 1 FROM claims WHERE symbol=? AND (entry_at=? OR "
            "(entry_at<? AND COALESCE(active_until,expires_at)>?)) LIMIT 1",
            (selected["symbol"], selected["entry_at"],
             selected["expires_at"], selected["entry_at"]),
        ).fetchone():
            return False
        cursor = db.execute(
            "INSERT OR IGNORE INTO claims "
            "(symbol,direction,entry_at,expires_at,source,claimed_at,active_until) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                selected["symbol"],
                selected["direction"],
                selected["entry_at"],
                selected["expires_at"],
                source,
                time.time() if now is None else now,
                selected["expires_at"],
            ),
        )
        db.commit()
        return cursor.rowcount == 1


def completed(path, selected, source, finished_at):
    """Shorten an acknowledged cycle only after a terminal result is saved.

    An uncertain Telegram send must not be released here. The claim remains
    permanent for its original entry minute, even if its cycle ended early.
    """
    if not Path(path).is_file():
        return False
    with closing(sqlite3.connect(path, timeout=1)) as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'"
        ).fetchone():
            return False
        columns = {row[1] for row in db.execute("PRAGMA table_info(claims)")}
        if "active_until" not in columns:
            db.execute("ALTER TABLE claims ADD COLUMN active_until REAL")
        cursor = db.execute(
            "UPDATE claims SET active_until=MIN(COALESCE(active_until,expires_at),?) "
            "WHERE symbol=? AND entry_at=? AND source=? "
            "AND ?>=entry_at AND ?<=expires_at",
            (finished_at, selected["symbol"], selected["entry_at"], source,
             finished_at, finished_at),
        )
        db.commit()
        return bool(cursor.rowcount)
