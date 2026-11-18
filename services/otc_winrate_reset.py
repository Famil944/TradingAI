"""Persistent owner-authorized reporting baseline for S1-S4 WIN/LOSS.

The original journals are immutable: reset affects only displayed aggregate
statistics, not signal generation, subscriber delivery, or outcome evaluation.
"""
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path


CONFIRM_SECONDS = 300


class WinrateReset:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    cutoff REAL NOT NULL DEFAULT 0,
                    nonce TEXT,
                    requested_at REAL
                );
                INSERT OR IGNORE INTO settings(id,cutoff) VALUES(1,0);
            """)
            db.commit()

    def cutoff(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            row = db.execute("SELECT cutoff FROM settings WHERE id=1").fetchone()
        return float(row[0]) if row else 0.0

    def prepare(self, now=None):
        now = time.time() if now is None else float(now)
        token = secrets.token_hex(8)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute(
                "UPDATE settings SET nonce=?,requested_at=? WHERE id=1",
                (token, now),
            )
            db.commit()
        return token

    def confirm(self, token, now=None):
        """Perform exactly one reset, within 5 min of an owner confirmation."""
        now = time.time() if now is None else float(now)
        if not isinstance(token, str) or len(token) != 16:
            return False
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT nonce,requested_at,cutoff FROM settings WHERE id=1"
            ).fetchone()
            if (
                not row or row[0] != token or row[1] is None
                or not 0 <= now - row[1] <= CONFIRM_SECONDS
            ):
                db.rollback()
                return False
            db.execute(
                "UPDATE settings SET cutoff=?,nonce=NULL,requested_at=NULL WHERE id=1",
                (max(now, row[2]),),
            )
            db.commit()
        return True

    def cancel(self, token):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            changed = db.execute(
                "UPDATE settings SET nonce=NULL,requested_at=NULL "
                "WHERE id=1 AND nonce=?",
                (token,),
            ).rowcount
            db.commit()
        return bool(changed)
