"""Durable provider OHLC archive with provenance, revisions and actual observation times."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path


class MarketArchive:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=20)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bars(symbol TEXT,time INTEGER,open REAL,high REAL,low REAL,close REAL,
                    first_observed REAL,last_observed REAL,revisions INTEGER DEFAULT 0,source TEXT,
                    PRIMARY KEY(symbol,time));
                CREATE TABLE IF NOT EXISTS catalogue(observed_at REAL,symbol TEXT,payload TEXT,
                    PRIMARY KEY(observed_at,symbol));
                CREATE TABLE IF NOT EXISTS attestations(recorded_at REAL PRIMARY KEY,statement TEXT);
            """)
            db.commit()

    def attest(self, now, statement):
        with closing(sqlite3.connect(self.path, timeout=20)) as db:
            db.execute("INSERT INTO attestations VALUES(?,?)", (now, statement))
            db.commit()

    def bars(self, symbol, bars, now, source="otcharts_history"):
        # A history request may return up to 5000 candles. Fetch the current
        # rows once instead of issuing a SELECT per candle on a slow phone.
        valid = [bar for bar in bars if bar.time + 60 <= now]
        if not valid:
            return
        with closing(sqlite3.connect(self.path, timeout=20)) as db:
            original = {
                at: (op, hi, lo, cl, src)
                for at, op, hi, lo, cl, src in db.execute(
                    "SELECT time,open,high,low,close,source FROM bars "
                    "WHERE symbol=? AND time BETWEEN ? AND ?",
                    (symbol, min(bar.time for bar in valid), max(bar.time for bar in valid)),
                )
            }
            for bar in valid:
                values = (bar.open, bar.high, bar.low, bar.close)
                old = original.get(bar.time)
                if (
                    old is not None
                    and old[4] == "otcharts_history"
                    and source == "otcharts_ticks"
                ):
                    continue
                if old is None:
                    db.execute(
                        "INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (symbol, bar.time, *values, now, now, 0, source),
                    )
                    original[bar.time] = (*values, source)
                elif tuple(old[:4]) != values:
                    # Tick-built bars are provisional; official history replaces them without inventing platform proof.
                    revision = (
                        0
                        if old[4] == "otcharts_ticks" and source == "otcharts_history"
                        else 1
                    )
                    db.execute(
                        "UPDATE bars SET open=?,high=?,low=?,close=?,last_observed=?,revisions=revisions+?,source=? WHERE symbol=? AND time=?",
                        (*values, now, revision, source, symbol, bar.time),
                    )
                    original[bar.time] = (*values, source)
                elif old[4] == "otcharts_ticks" and source == "otcharts_history":
                    # Identical OHLC still gains canonical provenance on confirmation.
                    db.execute(
                        "UPDATE bars SET source=?,last_observed=? WHERE symbol=? AND time=?",
                        (source, now, symbol, bar.time),
                    )
                    original[bar.time] = (*values, source)
            db.commit()

    def assets(self, assets, now):
        with closing(sqlite3.connect(self.path, timeout=20)) as db:
            db.executemany(
                "INSERT OR IGNORE INTO catalogue VALUES(?,?,?)",
                [
                    (now, a.symbol, json.dumps(asdict(a), allow_nan=False))
                    for a in assets
                ],
            )
            db.commit()

    def export(self, destination):
        with closing(sqlite3.connect(self.path, timeout=20)) as db:
            db.row_factory = sqlite3.Row
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM bars WHERE revisions=0 ORDER BY symbol,time"
                )
            ]
        destination = Path(destination)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            "".join(
                json.dumps(
                    dict(
                        r,
                        ohlc_source=r["source"],
                        source="otcharts",
                        venue="otc",
                        is_otc=True,
                    ),
                    allow_nan=False,
                )
                + "\n"
                for r in rows
            ),
            encoding="utf-8",
        )
        temporary.replace(destination)
        return len(rows)
