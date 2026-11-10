"""Additive SQLite journal. Existing user tables are never dropped."""

import json
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path


class Database:
    def __init__(self, db_path=None):
        if db_path is None:
            from config.settings import ROOT, settings

            p = Path(settings.database_path).expanduser()
            self.db_path = p if p.is_absolute() else ROOT / p
        else:
            self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init_db(self):
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='otc_journal_meta'"
            ).fetchone()
            old_tables = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
            columns = conn.execute("PRAGMA table_info(otc_journal_signals)").fetchall()
            nullable_migration = any(
                r["name"] in ("payout", "payout_at") and r["notnull"] for r in columns
            )
            if old_tables and (not exists or nullable_migration):
                backup = self.db_path.with_name(
                    self.db_path.name + f".backup-{time.time_ns()}"
                )
                with closing(sqlite3.connect(backup)) as dest:
                    conn.backup(dest)
            if nullable_migration:
                # Rebuild only our journal, atomically, with every column/row preserved.
                ddl = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name='otc_journal_signals'"
                ).fetchone()[0]
                ddl = re.sub(
                    r"\b(payout|payout_at)\s+REAL\s+NOT\s+NULL",
                    r"\1 REAL",
                    ddl,
                    flags=re.I,
                )
                ddl = ddl.replace(
                    "otc_journal_signals", "otc_journal_signals_nullable", 1
                )
                auxiliary = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name='otc_journal_signals' AND type IN ('index','trigger') AND sql IS NOT NULL"
                ).fetchall()
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(ddl)
                names = ",".join(
                    '"' + r["name"].replace('"', '""') + '"' for r in columns
                )
                conn.execute(
                    f"INSERT INTO otc_journal_signals_nullable ({names}) SELECT {names} FROM otc_journal_signals"
                )
                conn.execute("DROP TABLE otc_journal_signals")
                conn.execute(
                    "ALTER TABLE otc_journal_signals_nullable RENAME TO otc_journal_signals"
                )
                for statement in auxiliary:
                    conn.execute(statement[0])
                conn.commit()
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS otc_journal_meta (version INTEGER NOT NULL);
            INSERT INTO otc_journal_meta SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM otc_journal_meta);
            CREATE TABLE IF NOT EXISTS otc_journal_signals (
              id TEXT PRIMARY KEY, symbol TEXT NOT NULL, direction TEXT NOT NULL,
              timeframe INTEGER NOT NULL, strategy TEXT NOT NULL, source TEXT NOT NULL,
              signal_at REAL NOT NULL, sent_at REAL, intended_entry_at REAL NOT NULL,
              actual_entry_at REAL, expires_at REAL NOT NULL, price REAL NOT NULL,
              entry_price REAL, expiry_price REAL, payout REAL, payout_at REAL,
              reason TEXT NOT NULL, quality TEXT NOT NULL, synchronized INTEGER NOT NULL,
              outcome TEXT NOT NULL DEFAULT 'PENDING', delivery TEXT NOT NULL DEFAULT 'PENDING',
              UNIQUE(symbol,timeframe,signal_at,strategy)
            );
            UPDATE otc_journal_meta SET version=2;
            CREATE INDEX IF NOT EXISTS otc_pending_idx ON otc_journal_signals(outcome,symbol,expires_at);
            CREATE TABLE IF NOT EXISTS otc_journal_events (
              id INTEGER PRIMARY KEY, at REAL NOT NULL, symbol TEXT, reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS otc_journal_comparisons (
              id INTEGER PRIMARY KEY, at REAL NOT NULL, source TEXT NOT NULL,
              symbol TEXT NOT NULL, provider_at REAL NOT NULL, platform_at REAL NOT NULL,
              provider_price REAL NOT NULL, platform_price REAL NOT NULL,
              delay REAL NOT NULL, deviation REAL NOT NULL, evidence TEXT NOT NULL
            );
            """)

    def event(self, reason, symbol=None, at=None):
        with self.connect() as c:
            c.execute(
                "INSERT INTO otc_journal_events(at,symbol,reason) VALUES(?,?,?)",
                (time.time() if at is None else at, symbol, reason),
            )
            c.execute(
                "DELETE FROM otc_journal_events WHERE id <= (SELECT COALESCE(MAX(id),0)-10000 FROM otc_journal_events)"
            )

    def record(
        self,
        *,
        symbol,
        direction,
        timeframe,
        signal_at,
        price,
        payout,
        payout_at,
        reason,
        quality,
        source,
        synchronized=False,
        strategy="ema-rsi-atr-v1",
        cooldown=900,
    ):
        identifier = str(uuid.uuid4())
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            last = c.execute(
                "SELECT signal_at FROM otc_journal_signals WHERE symbol=? AND timeframe=? ORDER BY signal_at DESC LIMIT 1",
                (symbol, timeframe),
            ).fetchone()
            if last and signal_at - last[0] < cooldown:
                return None
            try:
                c.execute(
                    """INSERT INTO otc_journal_signals
                  (id,symbol,direction,timeframe,strategy,source,signal_at,intended_entry_at,expires_at,
                   price,payout,payout_at,reason,quality,synchronized)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        identifier,
                        symbol,
                        direction,
                        timeframe,
                        strategy,
                        source,
                        signal_at,
                        signal_at,
                        signal_at + timeframe,
                        price,
                        payout,
                        payout_at,
                        reason,
                        json.dumps(quality),
                        int(synchronized),
                    ),
                )
            except sqlite3.IntegrityError:
                return None
        return identifier

    def delivered(self, identifier, at=None, success=True):
        with self.connect() as c:
            c.execute(
                "UPDATE otc_journal_signals SET sent_at=?, delivery=? WHERE id=?",
                (at if success else None, "SENT" if success else "FAILED", identifier),
            )

    def pending(self, symbol=None):
        with self.connect() as c:
            sql = "SELECT * FROM otc_journal_signals WHERE outcome='PENDING'"
            return [
                dict(r)
                for r in c.execute(
                    sql + (" AND symbol=?" if symbol else ""),
                    (symbol,) if symbol else (),
                )
            ]

    def capture_entry(self, identifier, at, price):
        with self.connect() as c:
            c.execute(
                "UPDATE otc_journal_signals SET actual_entry_at=?,entry_price=? WHERE id=? AND actual_entry_at IS NULL",
                (at, price, identifier),
            )

    def finish(self, identifier, outcome, price=None):
        if outcome not in {"WIN", "LOSS", "DRAW", "UNKNOWN"}:
            raise ValueError("Invalid outcome")
        with self.connect() as c:
            c.execute(
                "UPDATE otc_journal_signals SET outcome=?,expiry_price=? WHERE id=? AND outcome='PENDING'",
                (outcome, price, identifier),
            )

    def recover(self):
        # A restart cannot prove continuity or the actual entry time.
        with self.connect() as c:
            c.execute(
                "UPDATE otc_journal_signals SET outcome='UNKNOWN' WHERE outcome='PENDING'"
            )

    def compare(
        self,
        *,
        source,
        symbol,
        provider_at,
        platform_at,
        provider_price,
        platform_price,
        evidence,
        at=None,
    ):
        if not evidence or min(provider_price, platform_price) <= 0:
            raise ValueError("Comparison needs evidence and positive prices")
        with self.connect() as c:
            c.execute(
                """INSERT INTO otc_journal_comparisons
            (at,source,symbol,provider_at,platform_at,provider_price,platform_price,delay,deviation,evidence)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.time() if at is None else at,
                    source,
                    symbol,
                    provider_at,
                    platform_at,
                    provider_price,
                    platform_price,
                    abs(provider_at - platform_at),
                    abs(provider_price - platform_price) / platform_price,
                    evidence,
                ),
            )

    def stats(self, since=0):
        with self.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    """SELECT symbol,timeframe,strategy,COUNT(*) AS total,
            SUM(outcome='WIN') AS wins,SUM(outcome='LOSS') AS losses,SUM(outcome='DRAW') AS draws,
            SUM(outcome='UNKNOWN') AS unknown,SUM(outcome='PENDING') AS pending,
            AVG(CASE WHEN outcome='WIN' THEN payout/100.0 WHEN outcome='LOSS' THEN -1.0 WHEN outcome='DRAW' THEN 0 END) AS expectancy
            FROM otc_journal_signals WHERE signal_at>=? GROUP BY symbol,timeframe,strategy""",
                    (since,),
                )
            ]
