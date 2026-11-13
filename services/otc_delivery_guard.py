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
