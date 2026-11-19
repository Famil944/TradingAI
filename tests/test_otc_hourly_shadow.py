"""Causal H1 shadow tests: a single 60-minute result, never Telegram delivery."""
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.otc_candidate_research import TickSampler
from services.otc_early_lead import EarlyLeadObserver, early_winrate, format_early_winrate
from services.otc_hourly_shadow import (
    H1_SECONDS, H1_WINDOW, H1_VERSION,
    hourly_candidate, hourly_entry, hourly_outcome,
    observed_hourly_minutes,
)
from services.otc_market import Asset, Tick
from services.otc_strategy import Candle, aggregate

ENTRY = 151 * 3600
NOW = ENTRY - 295
ASOF = int(NOW // 60) * 60
SYM = "A_otc"


def bar(at):
    # Valid, nonflat candles.
    base = 100 + 0.01 * ((at // 60) % 31)
    return Candle(at, base, base + 0.1, base - 0.1, base + 0.02)


def snapshot(hours=22):
    minutes = [bar(t) for t in range(
        ENTRY - hours * 3600, ASOF, 60
    )]
    asset = Asset(SYM, True, True, 93, NOW, "A OTC", NOW)
    return ({SYM: minutes}, {SYM: asset}, {SYM: NOW}, None)


def shadow_pattern():
    return (
        patch("services.otc_hourly_shadow.pattern_directions", return_value={
            "bollinger_reversal": {"direction": "UP"},
        }),
        patch("services.otc_hourly_shadow.community_signals", return_value={}),
    )


class HourlyCausalityTests(unittest.TestCase):
    def test_h1_aggregates_complete_minutes_only_and_keeps_missing_hours_missing(self):
        hours = [bar(t) for t in range(ENTRY - 65 * 3600, ENTRY - 3600, 60)]
        result = aggregate(hours, 3600)
        self.assertEqual(len(result), 64)
        self.assertTrue(all(b.time % 3600 == 0 for b in result))
        missing = [x for x in hours if x.time != ENTRY - 24 * 3600]
        other = aggregate(missing, 3600)
        self.assertEqual(len(other), 63)
        self.assertNotIn(ENTRY - 24 * 3600, [b.time for b in other])
        self.assertEqual(len(aggregate(hours[-59:], 3600)), 0)

    def test_forecast_exact_5_minute_window_and_hour_boundary(self):
        p1, p2 = shadow_pattern()
        with tempfile.TemporaryDirectory() as root, p1 as p, p2:
            s = snapshot()
            proposal = hourly_candidate(s, NOW, Path(root) / "noarchive.db")
            self.assertIsNotNone(proposal)
            self.assertEqual(proposal["entry_at"], ENTRY)
            self.assertEqual(proposal["expires_at"], ENTRY + 3600)
            self.assertEqual(proposal["entry_at"] - proposal["observed_at"], 295)
            self.assertEqual(proposal["experiment"]["strategy"], "H1")
            self.assertEqual(proposal["experiment"]["attempts"], 1)
            self.assertEqual(proposal["experiment"]["source_strategy"], "S1")
            self.assertEqual(p.call_args.args[1:3], (3600, ASOF))
            self.assertEqual(hourly_entry(NOW), ENTRY)
            self.assertIsNone(hourly_candidate(s, ENTRY - 305, Path(root)/"x"))
            self.assertIsNone(hourly_candidate(s, ENTRY - 284, Path(root)/"x"))
            bad = (s[0], s[1], {SYM: NOW - 9}, s[3])
            self.assertIsNone(hourly_candidate(bad, NOW, Path(root)/"x"))
            no_end = (s[0], s[1], s[2], ENTRY + 3500)
            self.assertIsNone(hourly_candidate(no_end, NOW, Path(root)/"x"))

    def test_archive_does_not_accept_retroactively_imported_history(self):
        with tempfile.TemporaryDirectory() as root:
            dbpath = Path(root) / "archive.db"
            with sqlite3.connect(dbpath) as db:
                db.execute("CREATE TABLE bars("
                    "symbol TEXT,time INTEGER,open REAL,high REAL,low REAL,close REAL,"
                    "first_observed REAL,last_observed REAL,revisions INTEGER,source TEXT)")
                db.executemany("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)", [
                    (SYM, ENTRY - 70 * 3600 + i * 60, 100, 101, 99, 100.5,
                     NOW + 60 if i == 0 else NOW - 30, NOW - 1, 0, "otcharts_history")
                    for i in range(60)
                ])
            seen = observed_hourly_minutes(dbpath, SYM, ENTRY, NOW)
            self.assertEqual(len(seen), 59)
            self.assertNotIn(ENTRY - 70 * 3600, seen)

    def test_real_forecaster_uses_existing_causal_guard_for_h1(self):
        p1, p2 = shadow_pattern()
        with tempfile.TemporaryDirectory() as root:
            s = snapshot(2)
            # Not enough full hours; the REAL pattern function refuses.
            with p2:
                self.assertIsNone(hourly_candidate(s, NOW, Path(root)/"archive.db"))
            # Twenty full H1 candles are enough for S1's original function
            # to be called; no special H1 threshold reduction is applied.
            from services.otc_shadow_variants import pattern_directions
            self.assertEqual(
                pattern_directions(s[0][SYM], 3600, ASOF),
                {},
            )

    def test_one_hour_only_outcome_and_unknown_remain_conservative(self):
        proposal = dict(
            symbol=SYM, direction="UP", entry_at=ENTRY,
            expires_at=ENTRY + H1_SECONDS,
        )
        with tempfile.TemporaryDirectory() as root:
            archive = Path(root) / "does-not-exist.db"
            prices = {(SYM, ENTRY): 100, (SYM, ENTRY+3600): 102}
            self.assertEqual(hourly_outcome(proposal, prices, ENTRY + 100, archive), "PENDING")
            self.assertEqual(hourly_outcome(proposal, prices, ENTRY + 3601, archive), "WIN")
            self.assertEqual(hourly_outcome(dict(proposal, direction="DOWN"),
                                            prices, ENTRY + 3601, archive), "LOSS")
            self.assertEqual(hourly_outcome(proposal, {}, ENTRY + 3601, archive), "PENDING")
            self.assertEqual(hourly_outcome(proposal, {}, ENTRY + 3900, archive), "UNKNOWN")


class HourlyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_shadow_runtime_records_only_one_hourly_candidate_without_telegram(self):
        p1, p2 = shadow_pattern()
        with tempfile.TemporaryDirectory() as root, p1, p2:
            time_value = [NOW]
            observer = EarlyLeadObserver(root, clock=lambda: time_value[0])
            session = SimpleNamespace(running=lambda: True, transport="live")
            cycles = SimpleNamespace(enabled=True, allowed_timeframes=(120,))
            observer.observe(*snapshot())
            await observer.tick(session, cycles)
            await observer.tick(session, cycles)
            self.assertEqual(observer.counters["hourly_recorded"], 1)
            self.assertEqual(observer.journal.pending_future(NOW)[0]["version"], H1_VERSION)
            sampler = TickSampler()
            hourly = observer.journal.pending_future(NOW)
            sampler.register(hourly, NOW)
            self.assertIn(ENTRY, sampler.targets[SYM])
            self.assertIn(ENTRY + 3600, sampler.targets[SYM])
            self.assertNotIn(ENTRY + 7200, sampler.targets[SYM])
            time_value[0] = ENTRY
            observer.observe_tick(Tick(SYM, ENTRY, 100), ENTRY)
            time_value[0] = ENTRY + 3600
            observer.observe_tick(Tick(SYM, ENTRY + 3600, 101), ENTRY + 3600)
            await observer.flush()
            stats = early_winrate(root, since=0, now=ENTRY + 3602)
            self.assertEqual(stats["hourly"]["WIN"], 1)
            self.assertEqual(stats["overall"]["WIN"], 0)
            report = format_early_winrate(stats)
            self.assertIn("🕐 H1", report)
            self.assertIn("✅ 1 / ❌ 0 · WR 100.0%", report)
            self.assertFalse((Path(root) / "data" / "community-live.db").exists())
            self.assertFalse((Path(root) / "data" / "bollinger-cycles.db").exists())


if __name__ == "__main__":
    unittest.main()
