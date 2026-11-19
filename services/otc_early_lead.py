"""Prospective 30-40s OTC timing experiment, strictly shadow-only.

No Telegram messages, orders, API requests, trading claims, or live journal writes.
The existing S1 and S2-S4 signal functions and data gates are reused unchanged.
"""
import argparse
import asyncio
import json
import logging
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from services.otc_candidate_research import CandidateJournal, TickSampler, finite_payload
from services.otc_community_cycles import signals as community_signals
from services.otc_delivery_guard import check_data
from services.otc_groq import fresh
from services.otc_ml.preview import aligned_entry
from services.otc_shadow_variants import pattern_directions
from services.otc_strategies_live import evaluate_cycle
from services.otc_validation_report import load

LOG = logging.getLogger(__name__)
VERSION = "early-lead-30-40-v1"
WINDOW = (30, 40)
METHODS = {"bb_reentry": "S2", "break_retest": "S3", "ema_pullback": "S4"}
STRATEGIES = ("S1", "S2", "S3", "S4")
PERIODS = (120, 300, 900)


def candidate_rows(snapshot, now, allowed=PERIODS):
    """Generate only from data that existed when the early observation occurred.

    Both early and live modes use the same entry minute and feature cutoff.
    Never shift the input timestamp forward to pretend the next candle closed.
    """
    if snapshot is None:
        return []
    histories, assets, quotes, end = snapshot
    entry = int(now) // 60 * 60 + 60
    lead = entry - now
    if not WINDOW[0] <= lead <= WINDOW[1]:
        return []
    ranked = {}
    for seconds in allowed:
        if seconds not in PERIODS or not aligned_entry(entry, seconds):
            continue
        if end and entry + 3 * seconds + 2 > end:
            continue
        for symbol, minutes in histories.items():
            asset = assets.get(symbol)
            if not fresh(asset, quotes.get(symbol), now):
                continue
            base = dict(
                symbol=symbol, name=asset.name or symbol,
                entry_at=entry, expires_at=entry + 3 * seconds,
                cycle_seconds=seconds, feature_asof=entry - 60,
                observed_at=now, payout=asset.payout, gates={"cycle": True},
            )
            if check_data(base, snapshot, now):
                continue
            # S1 and S2-S4 use the exact same pure forecast functions as live.
            s1 = pattern_directions(list(minutes), seconds, entry - 60).get(
                "bollinger_reversal"
            )
            choices = {"S1": s1} if s1 else {}
            choices.update({
                METHODS[method]: decision
                for method, decision in community_signals(
                    list(minutes), seconds, entry - 60
                ).items() if method in METHODS
            })
            for strategy, pattern in choices.items():
                if pattern["direction"] not in ("UP", "DOWN"):
                    continue
                row = dict(
                    base, version=VERSION + ":" + strategy,
                    direction=pattern["direction"],
                    experiment=dict(
                        version=VERSION, strategy=strategy,
                        cycle_seconds=seconds, lead_window=list(WINDOW),
                        shadow_only=True,
                    ),
                )
                key = strategy, seconds, entry
                # Deterministic candidate selection matches live's payout
                # ranking, before either timing arm's own cycle exclusions.
                priority = (-asset.payout, symbol)
                if key not in ranked or priority < ranked[key][0]:
                    ranked[key] = (priority, row)
    return [item[1] for key, item in sorted(ranked.items())]


class EarlyJournal:
    """Separate SQLite store. Existing forecasting journals remain untouched."""

    def __init__(self, path):
        self.path = Path(path)
        self.candidates = CandidateJournal(self.path)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS early_slots(
                    strategy TEXT NOT NULL,seconds INTEGER NOT NULL,
                    entry_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,
                    symbol TEXT NOT NULL,direction TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    PRIMARY KEY(strategy,seconds,entry_at)
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS early_lock "
                "ON early_slots(strategy,seconds,entry_at,expires_at)"
            )
            db.commit()

    def record(self, rows):
        accepted = []
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute("BEGIN IMMEDIATE")
            for row in rows:
                strategy = row["experiment"]["strategy"]
                seconds = int(row["cycle_seconds"])
                entry = int(row["entry_at"])
                expires = int(row["expires_at"])
                if db.execute(
                    "SELECT 1 FROM early_slots WHERE strategy=? AND seconds=? "
                    "AND entry_at<? AND expires_at>? LIMIT 1",
                    (strategy, seconds, expires, entry),
                ).fetchone():
                    continue
                inserted = db.execute(
                    "INSERT OR IGNORE INTO early_slots VALUES(?,?,?,?,?,?,?)",
                    (strategy, seconds, entry, expires, row["symbol"],
                     row["direction"], row["observed_at"]),
                ).rowcount
                if not inserted:
                    continue
                db.execute(
                    "INSERT INTO candidates "
                    "(version,symbol,entry_at,expires_at,observed_at,payload) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        row["version"], row["symbol"], entry, expires,
                        row["observed_at"],
                        json.dumps(finite_payload(row), ensure_ascii=False,
                                   allow_nan=False),
                    ),
                )
                accepted.append(row)
            db.commit()
        return accepted

    def pending_future(self, now):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            rows = db.execute(
                "SELECT payload FROM candidates WHERE expires_at>? "
                "AND observed_at<entry_at ORDER BY entry_at", (now,),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]


class EarlyLeadObserver:
    def __init__(self, root, clock=time.time):
        self.root = Path(root)
        self.clock = clock
        self.journal = EarlyJournal(self.root / "data" / "early-lead-30-40.db")
        self.sampler = TickSampler()
        self.snapshot = None
        self.session_end = None
        self.last_plan_at = None
        self.next_flush = 0
        self.counters = Counter()
        self.next_report = clock() + 300
        self.sampler.register(self.journal.pending_future(self.clock()), self.clock())

    def observe(self, history, assets, quotes, end):
        # A separate snapshot of the *same* provider feed, not a new stream.
        self.snapshot = (history, assets, quotes, end)

    def observe_tick(self, tick, now):
        self.sampler.observe(tick, now)

    async def flush(self):
        data = self.sampler.drain(self.clock())
        if data:
            try:
                await asyncio.to_thread(self.journal.candidates.samples, data)
            except Exception:
                self.sampler.restore(data)
                raise

    async def tick(self, session, cycles):
        now = self.clock()
        if now >= self.next_flush:
            self.next_flush = now + 2
            await self.flush()
        if now >= self.next_report:
            LOG.info("OTC early 30-40s shadow diagnostics (last 300s): %s",
                     dict(self.counters))
            self.counters.clear()
            self.next_report = now + 300
        if not (session.running() and getattr(cycles, "enabled", False)
                and self.snapshot is not None and session.transport == "live"):
            return
        entry = int(now) // 60 * 60 + 60
        if not WINDOW[0] <= entry - now <= WINDOW[1]:
            return
        # Give transiently stale quotes a few seconds to recover inside the
        # same early window. Up to four scans, never a backfilled forecast.
        if (self.last_plan_at is not None
                and self.last_plan_at[0] == entry
                and now - self.last_plan_at[1] < 3):
            return
        self.last_plan_at = (entry, now)
        history, assets, quotes, end = self.snapshot
        frozen = ({s: list(b) for s, b in history.items()},
                  dict(assets), dict(quotes), end)
        allowed = tuple(getattr(cycles, "allowed_timeframes", PERIODS))
        rows = await asyncio.to_thread(candidate_rows, frozen, now, allowed)
        if not WINDOW[0] <= entry - self.clock() <= WINDOW[1]:
            self.counters["late_computation"] += 1
            return
        recorded = await asyncio.to_thread(self.journal.record, rows)
        self.sampler.register(recorded, self.clock())
        self.counters["planning_windows"] += 1
        self.counters["candidates"] += len(rows)
        self.counters["recorded"] += len(recorded)
        if rows:
            LOG.info("OTC early 30-40s shadow: entry=%d lead=%.1fs "
                     "candidates=%d recorded=%d (no Telegram delivery)",
                     entry, entry - now, len(rows), len(recorded))

    async def run(self, session, cycles):
        try:
            while True:
                try:
                    await self.tick(session, cycles)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.warning("OTC early shadow skipped: %s", type(exc).__name__)
                await asyncio.sleep(1)
        finally:
            try:
                await self.flush()
            except Exception:
                LOG.exception("OTC early shadow final flush failed")


def _live_rows(root, since):
    """Delivered live signals only; never include undelivered research candidates."""
    result = []
    root = Path(root)
    main = root / "data" / "bollinger-cycles.db"
    if main.is_file():
        with closing(sqlite3.connect(main.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            for symbol, entry, seconds, direction, outcome in db.execute(
                "SELECT symbol,entry_at,"
                "CASE WHEN json_valid(evidence) THEN "
                "json_extract(evidence,'$.cycle_seconds') END,"
                "direction,outcome FROM predictions "
                "WHERE sent_at>=? AND outcome NOT IN "
                "('NOT_DELIVERED','LATE','CANCELLED')", (since,),
            ):
                if seconds in PERIODS:
                    result.append(("S1", symbol, int(seconds), int(entry),
                                   direction, outcome))
    others = root / "data" / "community-live.db"
    if others.is_file():
        with closing(sqlite3.connect(others.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            for method, symbol, seconds, entry, direction, outcome in db.execute(
                "SELECT strategy,symbol,seconds,entry_at,direction,outcome "
                "FROM signals WHERE sent_at>=? AND outcome NOT IN "
                "('NOT_DELIVERED','LATE','CANCELLED','RESERVED')", (since,),
            ):
                strategy = METHODS.get(method)
                if strategy and seconds in PERIODS:
                    result.append((strategy, symbol, seconds, int(entry),
                                   direction, outcome))
    return result


def comparison(root, since=0, now=None):
    """Read-only report; matched forecasts compare identical market entry slots."""
    root = Path(root)
    now = time.time() if now is None else now
    path = root / "data" / "early-lead-30-40.db"
    rows, prices = load(path, since, now)
    early = {}
    for row in rows:
        exp = row.get("experiment") or {}
        strategy, seconds = exp.get("strategy"), exp.get("cycle_seconds")
        if exp.get("version") != VERSION or strategy not in STRATEGIES:
            continue
        if not isinstance(seconds, int) or seconds not in PERIODS:
            continue
        key = strategy, row["symbol"], seconds, int(row["entry_at"])
        _, _, outcome = evaluate_cycle(
            dict(row, seconds=seconds), prices, now,
            root / "data" / "market-history.db"
        )
        early[key] = (row["direction"], outcome, row["observed_at"])
    live = {
        (strategy, symbol, seconds, entry): (direction, outcome)
        for strategy, symbol, seconds, entry, direction, outcome
        in _live_rows(root, since)
    }
    groups = []
    for strategy in STRATEGIES:
        for seconds in PERIODS:
            e = {k: v for k, v in early.items()
                 if k[0] == strategy and k[2] == seconds}
            l = {k: v for k, v in live.items()
                 if k[0] == strategy and k[2] == seconds}
            shared = e.keys() & l.keys()
            agree = sum(e[k][0] == l[k][0] for k in shared)
            e_counts = Counter(v[1] for v in e.values())
            l_counts = Counter(v[1] for v in l.values())
            both_known = [k for k in shared
                          if e[k][1] in ("WIN", "LOSS") and
                          l[k][1] in ("WIN", "LOSS")]
            groups.append(dict(
                strategy=strategy, seconds=seconds,
                early=len(e), live=len(l), matched=len(shared),
                directions_agree=agree,
                early_outcomes=dict(e_counts), live_outcomes=dict(l_counts),
                paired_known=len(both_known),
                early_paired_win=sum(e[k][1] == "WIN" for k in both_known),
                live_paired_win=sum(l[k][1] == "WIN" for k in both_known),
            ))
    return groups


def format_report(groups):
    lines = [
        "Сравнение входа: ранний 30–40 с против основного 15–20 с / S2–S4 позже",
        "Ранние: только фоновые прогнозы; основные: только доставленные сигналы.",
        "Это виртуальные результаты OTCharts, не сделки Pocket Option.",
        "WINRATE = WIN/(WIN+LOSS), PENDING/DRAW/UNKNOWN исключены.",
        "",
    ]
    for g in groups:
        e, l = g["early_outcomes"], g["live_outcomes"]
        def rate(c):
            wins, losses = c.get("WIN", 0), c.get("LOSS", 0)
            return f"{100*wins/(wins+losses):.1f}%" if wins+losses else "—"
        n = g["paired_known"]
        matched = (
            f"{g['early_paired_win']}/{n} vs {g['live_paired_win']}/{n}"
            if n else "нет подтверждённых пар"
        )
        lines.append(
            f"{g['strategy']} M{g['seconds']//60}: "
            f"ранних {g['early']} (WR {rate(e)}), "
            f"основных {g['live']} (WR {rate(l)}); "
            f"совпадений по паре/времени {g['matched']}, "
            f"одно направление {g['directions_agree']}; "
            f"WIN по сравнимым исходам {matched}"
        )
    lines += [
        "",
        "Ранний прогноз не является отправленным сигналом. "
        "Две группы могут выбирать разные пары и иметь разный размер выборки.",
        "Нельзя считать ранний вариант точнее, пока нет достаточных "
        "сопоставимых подтверждённых наблюдений.",
    ]
    return "\n".join(lines)


def main():
    from config.settings import ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    if not args.report or not 1 <= args.days <= 365:
        parser.error("Запуск отчёта: --report --days 7")
    print(format_report(comparison(ROOT, time.time() - args.days * 86400)))


if __name__ == "__main__":
    main()
