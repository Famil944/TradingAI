"""Read-only verification of previously UNKNOWN Bollinger ×3 outcomes.

Never reprice, mutate or even open the virtual wallet. Original live tick
results remain in bollinger-cycles.db; recovered OTCharts OHLC outcomes are
stored in a separate provenance-aware SQLite database. This is retrospective
research, not proof of a Pocket Option execution price.
"""
import argparse
import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path

SOURCE = "otcharts_history"
VERSION = "bollinger-history-recovery-v1"
EXPIRIES = (120, 300, 900)


def _ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)


class RecoveryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS recovered(
                    signal_id TEXT PRIMARY KEY,
                    outcome TEXT NOT NULL,
                    attempts TEXT NOT NULL,
                    price_evidence TEXT NOT NULL,
                    source TEXT NOT NULL,
                    version TEXT NOT NULL,
                    recovered_at REAL NOT NULL
                );
            """)
            db.commit()

    def all(self):
        with closing(_ro(self.path)) as db:
            return {
                identifier: {
                    "outcome": result,
                    "attempts": json.loads(attempts),
                    "source": source,
                    "recovered_at": recorded,
                }
                for identifier, result, attempts, source, recorded in db.execute(
                    "SELECT signal_id,outcome,attempts,source,recovered_at FROM recovered"
                ).fetchall()
            }

    def get(self, identifier):
        with closing(_ro(self.path)) as db:
            row = db.execute(
                "SELECT outcome,attempts,source,recovered_at "
                "FROM recovered WHERE signal_id=?", (identifier,),
            ).fetchone()
            return (
                dict(
                    outcome=row[0], attempts=json.loads(row[1]),
                    source=row[2], recovered_at=row[3],
                )
                if row else None
            )

    def save(self, identifier, outcome, attempts, evidence, now):
        if outcome not in ("WIN", "LOSS", "DRAW"):
            raise ValueError("Cannot save unresolved historical outcome")
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO recovered"
                "(signal_id,outcome,attempts,price_evidence,source,version,recovered_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    identifier, outcome, json.dumps(attempts),
                    json.dumps(evidence, allow_nan=False), SOURCE, VERSION, now,
                ),
            )
            db.commit()
            return cursor.rowcount == 1


def _price(archive, symbol, target, now):
    # A one-minute bar's OPEN is the exact boundary price only in the canonical
    # source. Refuse tick-built, revised, future and incomplete candles.
    if target % 60 != 0 or target + 60 > now:
        return None
    record = archive.execute(
        "SELECT open,source,revisions,last_observed "
        "FROM bars WHERE symbol=? AND time=?", (symbol, target),
    ).fetchone()
    if not record:
        return None
    price, source, revisions, last_observed = record
    if (
        source != SOURCE or revisions != 0 or price is None
        or not math.isfinite(price) or price <= 0
        or last_observed is None or last_observed > now
    ):
        return None
    return {"time": target, "open": price, "source": source, "revisions": revisions}


def _determine(row, archive, now):
    try:
        payload = json.loads(row["evidence"] or "{}")
    except (ValueError, TypeError):
        return None
    seconds = payload.get("cycle_seconds")
    if seconds not in EXPIRIES or row["direction"] not in ("UP", "DOWN"):
        return None
    entry = row["entry_at"]
    if (
        entry is None or entry % 60 != 0
        or row["expires_at"] != entry + seconds * 3
        or row["sent_at"] is None
        or not 15 <= entry - row["sent_at"] <= 20
        or now < row["expires_at"] + 120
    ):
        return None
    attempts, proof = [], []
    for i in range(3):
        target = int(entry + seconds * i)
        opening = _price(archive, row["symbol"], target, now)
        closing = _price(archive, row["symbol"], target + seconds, now)
        if opening is None or closing is None:
            return None
        price_change = closing["open"] - opening["open"]
        state = (
            "DRAW" if price_change == 0
            else "WIN" if (price_change > 0) == (row["direction"] == "UP")
            else "LOSS"
        )
        attempts.append(state)
        proof.append({"attempt": i + 1, "entry": opening, "expiry": closing})
        if state != "LOSS":
            break
    return attempts[-1], attempts, proof


def recover_unknown(journal, archive_path, recovery_path, *, since=0, now=None, limit=500):
    """Safely inspect up to limit old UNKNOWN cycles, without touching the main DB."""
    now = time.time() if now is None else now
    journal, archive_path = Path(journal), Path(archive_path)
    if not journal.exists() or not archive_path.exists():
        return {"checked": 0, "recovered": 0, "unavailable": 0, "missing_files": True}
    store = RecoveryStore(recovery_path)
    existing = set(store.all())
    checked, recovered, unavailable = 0, 0, 0
    with closing(_ro(journal)) as db, closing(_ro(archive_path)) as history:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT id,symbol,entry_at,expires_at,direction,sent_at,evidence "
            "FROM predictions WHERE outcome='UNKNOWN' AND sent_at IS NOT NULL "
            "AND entry_at>=? AND expires_at <= ? ORDER BY entry_at DESC LIMIT ?",
            (since, now - 120, max(1, min(limit, 5000))),
        ).fetchall()
        for row in rows:
            identifier = row["id"]
            if identifier in existing:
                continue
            checked += 1
            result = _determine(row, history, now)
            if result is None:
                unavailable += 1
                continue
            final, attempts, evidence = result
            if store.save(identifier, final, attempts, evidence, now):
                recovered += 1
    return {
        "checked": checked, "recovered": recovered,
        "unavailable": unavailable, "missing_files": False,
    }


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", default="data/bollinger-cycles.db")
    parser.add_argument("--archive", default="data/market-history.db")
    parser.add_argument("--recovered", default="data/bollinger-result-recovery.db")
    parser.add_argument("--since", type=float, default=0)
    args = parser.parse_args()
    print(json.dumps(recover_unknown(
        args.journal, args.archive, args.recovered, since=args.since
    ), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()
