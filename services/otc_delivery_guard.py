"""Atomic cross-source delivery claims; ambiguous send failures retain the claim."""

import sqlite3
import time
from contextlib import closing


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



def delivery_snapshot(selected, snapshot, now, max_quote_age=8):
    """Final fail-closed quote/payout check immediately before Telegram delivery.

    Evidence is observational only. It cannot establish independent broker
    settlement or that the signal will arrive on the recipient's device.
    """
    import math

    if snapshot is None or not math.isfinite(now):
        return None
    _, assets, quotes, end = snapshot
    symbol = selected.get("symbol")
    asset = assets.get(symbol)
    last_quote = quotes.get(symbol)
    entry = selected.get("entry_at")
    expiry = selected.get("expires_at")
    if (
        asset is None or asset.open is not True
        or asset.payout is None or not 90 <= asset.payout <= 100
        or asset.observed_at is None or last_quote is None
        or not all(math.isfinite(value) for value in (last_quote, asset.observed_at, entry, expiry))
        or not 0 <= now - asset.observed_at <= 60
        or not 0 <= now - last_quote <= max_quote_age
        or not 15 <= entry - now <= 20
        or (end is not None and expiry + 60 > end)
    ):
        return None
    return {
        "checked_at": now,
        "quote_at": last_quote,
        "quote_age_s": round(now - last_quote, 3),
        "metadata_age_s": round(now - asset.observed_at, 3),
        "entry_lead_s": round(entry - now, 3),
        "platform_match_verified": False,
    }
