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
            conditional_missing = not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='otc_conditional_plans'"
            ).fetchone()
            assessment_missing = not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='otc_plan_assessments'"
            ).fetchone()
            context_missing = not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='otc_plan_context'"
            ).fetchone()
            entry_check_missing = not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='otc_entry_checks'"
            ).fetchone()
            if old_tables and (
                not exists
                or nullable_migration
                or conditional_missing
                or assessment_missing
                or context_missing
                or entry_check_missing
            ):
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
            CREATE TABLE IF NOT EXISTS otc_conditional_plans (
              id TEXT PRIMARY KEY, symbol TEXT NOT NULL, strategy TEXT NOT NULL,
              created_at REAL NOT NULL, setup_at REAL NOT NULL, entry_at REAL NOT NULL,
              expires_at REAL NOT NULL, condition_from REAL NOT NULL, delivery_deadline REAL NOT NULL,
              direction TEXT NOT NULL, colour TEXT NOT NULL, invalidation REAL NOT NULL,
              reason TEXT NOT NULL, payout REAL, payout_at REAL, sent_at REAL,
              delivery TEXT NOT NULL DEFAULT 'PENDING', lifecycle TEXT NOT NULL DEFAULT 'ACTIVE',
              outcome TEXT NOT NULL DEFAULT 'UNKNOWN',
              UNIQUE(symbol,entry_at,strategy)
            );
            CREATE TABLE IF NOT EXISTS otc_entry_checks (
              plan_id TEXT PRIMARY KEY, checked_at REAL NOT NULL, decision TEXT NOT NULL,
              evidence TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS otc_plan_context (
              plan_id TEXT PRIMARY KEY, known_at REAL NOT NULL, source TEXT NOT NULL,
              context TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS otc_plan_assessments (
              plan_id TEXT PRIMARY KEY, assessed_at REAL NOT NULL,
              outcome TEXT NOT NULL, condition_result TEXT NOT NULL,
              entry_price REAL, expiry_price REAL, source TEXT NOT NULL,
              reason TEXT NOT NULL, evidence TEXT NOT NULL, notified_at REAL
            );
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

    def prepare_plan(
        self, plan, now, payout=None, payout_at=None, cooldown=120, strategy=None
    ):
        from services.otc_conditional import STRATEGY

        strategy = strategy or STRATEGY
        seconds = plan.expires_at - plan.entry_at
        if seconds != 60:
            strategy = f"{strategy}:M{int(seconds) // 60}"

        identifier = str(uuid.uuid4())
        if now > plan.delivery_deadline:
            return None
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if seconds in (300, 900):
                outstanding = c.execute(
                    """SELECT 1 FROM otc_conditional_plans p
                    LEFT JOIN otc_plan_assessments a ON a.plan_id=p.id
                    WHERE p.expires_at-p.entry_at=?
                    AND p.delivery NOT IN ('FAILED','LATE') AND p.lifecycle!='CANCELLED'
                    AND (p.expires_at>? OR a.plan_id IS NULL) LIMIT 1""",
                    (seconds, now),
                ).fetchone()
                if outstanding:
                    return None
            active = c.execute(
                "SELECT 1 FROM otc_conditional_plans WHERE symbol=? AND lifecycle='ACTIVE' AND expires_at>? AND expires_at-entry_at=?",
                (plan.symbol, now, plan.expires_at - plan.entry_at),
            ).fetchone()
            last = c.execute(
                "SELECT MAX(created_at) FROM otc_conditional_plans WHERE expires_at-entry_at=?",
                (plan.expires_at - plan.entry_at,),
            ).fetchone()[0]
            if active or (last is not None and now - last < cooldown):
                return None
            try:
                c.execute(
                    """INSERT INTO otc_conditional_plans
                (id,symbol,strategy,created_at,setup_at,entry_at,expires_at,condition_from,delivery_deadline,direction,colour,invalidation,reason,payout,payout_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        identifier,
                        plan.symbol,
                        strategy,
                        now,
                        plan.setup_at,
                        plan.entry_at,
                        plan.expires_at,
                        plan.condition_from,
                        plan.delivery_deadline,
                        plan.setup.direction,
                        plan.setup.colour,
                        plan.setup.invalidation,
                        plan.setup.reason,
                        payout,
                        payout_at,
                    ),
                )
                evidence = getattr(plan.setup, "evidence", None)
                if evidence:
                    c.execute(
                        "INSERT INTO otc_plan_context(plan_id,known_at,source,context) VALUES(?,?,?,?)",
                        (
                            identifier,
                            now,
                            "otcharts",
                            json.dumps(evidence, ensure_ascii=False, allow_nan=False),
                        ),
                    )
            except sqlite3.IntegrityError:
                return None
        return identifier

    def plan_delivered(self, identifier, at, success):
        with self.connect() as c:
            c.execute(
                "UPDATE otc_conditional_plans SET sent_at=?, delivery=CASE WHEN ?=0 THEN 'FAILED' WHEN ?>delivery_deadline THEN 'LATE' ELSE 'SENT' END WHERE id=?",
                (at, int(success), at, identifier),
            )

    def close_plans(self, now, cancel=False):
        with self.connect() as c:
            if cancel:
                c.execute(
                    "UPDATE otc_conditional_plans SET lifecycle='CANCELLED' WHERE lifecycle='ACTIVE' AND entry_at>?",
                    (now,),
                )
                c.execute(
                    "UPDATE otc_conditional_plans SET lifecycle='EXPIRED' WHERE lifecycle='ACTIVE' AND expires_at<=?",
                    (now,),
                )
            else:
                c.execute(
                    "UPDATE otc_conditional_plans SET lifecycle='EXPIRED' WHERE lifecycle='ACTIVE' AND expires_at<=?",
                    (now,),
                )

    def assessment_targets(self, now):
        with self.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    """SELECT p.*,e.decision entry_decision FROM otc_conditional_plans p
              LEFT JOIN otc_entry_checks e ON e.plan_id=p.id
              LEFT JOIN otc_plan_assessments a ON a.plan_id=p.id WHERE a.plan_id IS NULL
              AND (p.lifecycle='CANCELLED' OR p.delivery IN ('FAILED','LATE') OR p.expires_at+65<=?)
              ORDER BY p.expires_at LIMIT 500""",
                    (now,),
                )
            ]

    def assess_plan(self, identifier, result, now):
        with self.connect() as c:
            cursor = c.execute(
                """INSERT OR IGNORE INTO otc_plan_assessments
              (plan_id,assessed_at,outcome,condition_result,entry_price,expiry_price,source,reason,evidence)
              VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    identifier,
                    now,
                    result["outcome"],
                    result["condition"],
                    result.get("entry_price"),
                    result.get("expiry_price"),
                    "otcharts_virtual_v1",
                    result["reason"],
                    json.dumps(result.get("evidence", {}), allow_nan=False),
                ),
            )
            return cursor.rowcount == 1

    def assessment_notifications(self, since):
        with self.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    """SELECT a.*,p.symbol,p.entry_at,p.expires_at FROM otc_plan_assessments a
            JOIN otc_conditional_plans p ON p.id=a.plan_id WHERE a.notified_at IS NULL AND p.expires_at>=?
            ORDER BY a.assessed_at LIMIT 3""",
                    (since,),
                )
            ]

    def assessment_notified(self, identifiers, now):
        with self.connect() as c:
            c.executemany(
                "UPDATE otc_plan_assessments SET notified_at=? WHERE plan_id=?",
                [(now, i) for i in identifiers],
            )

    def virtual_stats(self, since=0, timeframe=None):
        with self.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    """SELECT p.strategy,p.direction,p.colour,COUNT(*) total,
              SUM(a.outcome='WIN') wins,SUM(a.outcome='LOSS') losses,SUM(a.outcome='DRAW') draws,
              SUM(a.outcome='SKIPPED') skipped,SUM(a.outcome='UNKNOWN') unknown,
              SUM(a.outcome='CANCELLED') cancelled,SUM(a.outcome='NOT_DELIVERED') not_delivered,
              SUM(a.plan_id IS NULL) pending
              FROM otc_conditional_plans p LEFT JOIN otc_plan_assessments a ON a.plan_id=p.id
              WHERE p.created_at>=? AND (? IS NULL OR p.expires_at-p.entry_at=?) GROUP BY p.strategy,p.direction,p.colour""",
                    (since, timeframe, timeframe),
                )
            ]

    def plan_stats(self, since=0):
        with self.connect() as c:
            row = c.execute(
                "SELECT COUNT(*) total,SUM(delivery='SENT') sent,SUM(delivery='LATE') late,SUM(delivery='FAILED') failed,SUM(lifecycle='ACTIVE') active FROM otc_conditional_plans WHERE created_at>=?",
                (since,),
            ).fetchone()
            return dict(row)

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

    def entry_check_targets(self, now):
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                SELECT p.* FROM otc_conditional_plans p
                LEFT JOIN otc_entry_checks e ON e.plan_id=p.id
                WHERE e.plan_id IS NULL AND p.delivery='SENT'
                AND p.entry_at BETWEEN ? AND ? ORDER BY p.entry_at LIMIT 20
            """,
                    (now - 90, now - 2),
                )
            ]

    def claim_entry_check(self, identifier, now, decision, evidence):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT lifecycle FROM otc_conditional_plans WHERE id=?", (identifier,)
            ).fetchone()
            if row is None:
                return False
            if row["lifecycle"] == "CANCELLED":
                decision = "CANCELLED"
            inserted = connection.execute(
                "INSERT OR IGNORE INTO otc_entry_checks VALUES(?,?,?,?)",
                (identifier, now, decision, json.dumps(evidence, allow_nan=False)),
            ).rowcount
            return decision if inserted else False
