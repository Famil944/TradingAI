"""Prospective shadow evidence. No orders, network calls or inferred tick prices."""

import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

DELAYS = (0, 1, 3, 5)
SCHEMA = "ready-shadow-v1"


def finite_payload(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: finite_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_payload(item) for item in value]
    return value


class CandidateJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=1)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS candidates(
                    version TEXT,symbol TEXT,entry_at INTEGER,expires_at INTEGER,
                    observed_at REAL,payload TEXT,
                    PRIMARY KEY(version,symbol,entry_at,expires_at));
                CREATE TABLE IF NOT EXISTS samples(
                    symbol TEXT,target REAL,tick_at REAL,received_at REAL,price REAL,
                    PRIMARY KEY(symbol,target));
                CREATE INDEX IF NOT EXISTS candidate_time ON candidates(entry_at);
            """)

    def record(self, records):
        with closing(sqlite3.connect(self.path, timeout=0.5)) as db:
            db.executemany(
                "INSERT OR IGNORE INTO candidates VALUES(?,?,?,?,?,?)",
                [
                    (
                        r["version"],
                        r["symbol"],
                        r["entry_at"],
                        r["expires_at"],
                        r["observed_at"],
                        json.dumps(
                            finite_payload(dict(r, schema=SCHEMA)), allow_nan=False
                        ),
                    )
                    for r in records
                ],
            )
            db.commit()

    def samples(self, samples):
        with closing(sqlite3.connect(self.path, timeout=0.5)) as db:
            db.executemany("INSERT OR IGNORE INTO samples VALUES(?,?,?,?,?)", samples)
            db.commit()

    def settlement_prices(self, symbol, entry_at, expires_at):
        with closing(sqlite3.connect(self.path, timeout=0.5)) as db:
            rows = db.execute(
                """SELECT target,price FROM samples
                WHERE symbol=? AND target IN (?,?)""",
                (symbol, entry_at, expires_at),
            ).fetchall()
        prices = {target: price for target, price in rows}
        if entry_at not in prices or expires_at not in prices:
            return None
        entry, expiry = prices[entry_at], prices[expires_at]
        if not all(
            isinstance(value, (int, float))
            and math.isfinite(value)
            and value > 0
            for value in (entry, expiry)
        ):
            return None
        return entry, expiry


class TickSampler:
    """Sample first observed eligible ticks at targets, with a two-second tolerance.

    Registration must precede the target. Both event and receipt clocks must fit
    the tolerance; late/backfilled ticks cannot manufacture an executable price.
    Missing samples after restart intentionally remain missing.
    """

    def __init__(self):
        self.targets = {}
        self.buffer = {}

    @staticmethod
    def valid(tick, received_at):
        return (
            all(math.isfinite(v) for v in (tick.time, tick.price, received_at))
            and tick.price > 0
            and 0 <= received_at - tick.time <= 8
        )

    def register(self, records, now):
        for row in records:
            if not row.get("gates"):
                continue
            targets = [row["entry_at"] + d for d in DELAYS] + [row["expires_at"]]
            for target in targets:
                if target > now:
                    self.targets.setdefault(row["symbol"], set()).add(target)

    def observe(self, tick, received_at):
        if not self.valid(tick, received_at):
            return
        targets = self.targets.get(tick.symbol, set())
        for target in tuple(targets):
            if received_at > target + 2:
                targets.discard(target)
            elif target <= tick.time <= received_at <= target + 2:
                key = (tick.symbol, target)
                self.buffer.setdefault(key, (*key, tick.time, received_at, tick.price))
                targets.discard(target)

    def drain(self, now):
        self.targets = {
            symbol: {t for t in targets if t + 2 >= now}
            for symbol, targets in self.targets.items()
        }
        self.targets = {s: ts for s, ts in self.targets.items() if ts}
        samples = list(self.buffer.values())
        self.buffer.clear()
        return samples

    def restore(self, samples):
        for sample in samples:
            self.buffer.setdefault(tuple(sample[:2]), sample)
