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


def claim(path, selected, source, now=None, *, exclusive_timeframe=False):
    """Atomically reserve a delivery window.

    For S1–S4 (exclusive_timeframe=True), each strategy and expiry has its
    OWN three-attempt lock. Different strategies or M2/M5/M15 never block
    one another, even when they predict the same instrument/direction.

    Legacy callers keep the original cross-source same-symbol protection.
    Claims survive restarts and confirmed early WIN/DRAW/UNKNOWN outcomes:
    the strict scoped lock lasts until the scheduled third attempt ends.
    """
    duration = selected["expires_at"] - selected["entry_at"]
    if exclusive_timeframe and duration not in (3 * 120, 3 * 300, 3 * 900):
        raise ValueError("Exclusive live timeframe must be M2/M5/M15 x3")
    with closing(sqlite3.connect(path, timeout=1)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS claims(
            symbol TEXT, direction TEXT, entry_at INTEGER, expires_at INTEGER,
            source TEXT, claimed_at REAL,
            PRIMARY KEY(symbol,direction,entry_at,expires_at))""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(claims")}
        if "active_until" not in columns:
            db.execute("ALTER TABLE claims ADD COLUMN active_until REAL")
        db.execute(
            "CREATE INDEX IF NOT EXISTS claims_symbol_entry "
            "ON claims(symbol,entry_at)"
        )
        if exclusive_timeframe:
            # Use a separate table rather than rewriting the old primary key:
            # the legacy PK omits source, so it would wrongly block S2 if
            # S1 publishes on exactly the same symbol at the same time.
            db.execute("""CREATE TABLE IF NOT EXISTS strategy_timeframe_claims(
                source TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                claimed_at REAL NOT NULL,
                PRIMARY KEY(source,symbol,direction,entry_at,expires_at)
            )""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS stc_source_entry "
                "ON strategy_timeframe_claims(source,entry_at)"
            )
            # Keep previous-version active claims for the SAME strategy and
            # timeframe. Previously global claims from unrelated strategies
            # must not interfere after upgrading.
            overlaps = (
                db.execute(
                    "SELECT 1 FROM strategy_timeframe_claims "
                    "WHERE source=? AND (expires_at-entry_at)=? "
                    "AND entry_at<? AND expires_at>? LIMIT 1",
                    (source, duration, selected["expires_at"], selected["entry_at"]),
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM claims WHERE source=? "
                    "AND (expires_at-entry_at)=? "
                    "AND entry_at<? AND expires_at>? LIMIT 1",
                    (source, duration, selected["expires_at"], selected["entry_at"]),
                ).fetchone()
            )
            if overlaps:
                return False
            db.execute(
                "INSERT INTO strategy_timeframe_claims "
                "(source,symbol,direction,entry_at,expires_at,claimed_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    source, selected["symbol"], selected["direction"],
                    selected["entry_at"], selected["expires_at"],
                    time.time() if now is None else now,
                ),
            )
            db.commit()
            return True

        # Legacy behavior is deliberately unchanged for other claim callers.
        if db.execute(
            "SELECT 1 FROM claims WHERE symbol=? AND (entry_at=? OR "
            "(entry_at<? AND COALESCE(active_until,expires_at)>?)) LIMIT 1",
            (
                selected["symbol"], selected["entry_at"],
                selected["expires_at"], selected["entry_at"],
            ),
        ).fetchone():
            return False
        cursor = db.execute(
            "INSERT OR IGNORE INTO claims "
            "(symbol,direction,entry_at,expires_at,source,claimed_at,active_until) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                selected["symbol"], selected["direction"], selected["entry_at"],
                selected["expires_at"], source,
                time.time() if now is None else now, selected["expires_at"],
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
