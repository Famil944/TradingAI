"""Early 30-40s experiment must stay prospective and isolated from live delivery."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.otc_early_lead import (
    EarlyJournal, EarlyLeadObserver, candidate_rows,
    comparison, format_report,
)
from services.otc_market import Asset, Tick
from services.otc_strategy import Candle


NOW = 7165
ENTRY = 7200


def snapshot(now=NOW):
    bars = [
        Candle(ENTRY - 60 * n, 100, 102, 98, 101)
        for n in range(61, 0, -1)
    ]
    # Last genuine close is at 7140; 7140..7200 is still forming.
    asset = Asset("A_otc", True, True, 93, now, "A OTC", now)
    return ({"A_otc": bars}, {"A_otc": asset}, {"A_otc": now}, None)


def patched_patterns():
    return (
        patch("services.otc_early_lead.pattern_directions", return_value={
            "bollinger_reversal": {"direction": "UP"}
        }),
        patch("services.otc_early_lead.community_signals", return_value={
            "bb_reentry": {"direction": "UP"},
            "break_retest": {"direction": "DOWN"},
            "ema_pullback": {"direction": "UP"},
        }),
    )


class EarlyPlanningTests(unittest.TestCase):
    def test_all_four_forecasters_same_incomplete_minute_is_excluded(self):
        p1, p2 = patched_patterns()
        with p1 as boll, p2 as community:
            rows = candidate_rows(snapshot(), NOW)
        self.assertEqual(len(rows), 12)
        self.assertEqual({r["experiment"]["strategy"] for r in rows},
                         {"S1", "S2", "S3", "S4"})
        self.assertEqual({r["cycle_seconds"] for r in rows}, {120, 300, 900})
        self.assertTrue(all(r["feature_asof"] == ENTRY - 60 for r in rows))
        self.assertTrue(all(r["entry_at"] - r["observed_at"] == 35 for r in rows))
        self.assertTrue(all(r["experiment"]["shadow_only"] for r in rows))
        self.assertTrue(all(r["gates"] == {"cycle": True} for r in rows))
        self.assertTrue(all(
            args[2] == ENTRY - 60 for args, _ in boll.call_args_list
        ))
        self.assertTrue(all(
            args[2] == ENTRY - 60 for args, _ in community.call_args_list
        ))

    def test_original_15_20_window_rejected_and_future_closed_not_used(self):
        p1, p2 = patched_patterns()
        with p1, p2:
            self.assertEqual(candidate_rows(snapshot(7183), 7183), [])
            self.assertEqual(candidate_rows(snapshot(7157), 7157), [])
            # Payout metadata or latest tick too old is still rejected.
            s = snapshot()
            self.assertEqual(candidate_rows((s[0], s[1], {"A_otc": 7130}, None), NOW), [])
            s[0]["A_otc"] = s[0]["A_otc"][:-2]
            self.assertEqual(candidate_rows(s, NOW), [])

    def test_expiry_alignment_is_not_relaxed(self):
        p1, p2 = patched_patterns()
        with p1, p2:
            self.assertEqual(len(candidate_rows(snapshot(), NOW, (120,))), 4)
            s = snapshot()
            self.assertEqual(
                candidate_rows((s[0], s[1], s[2], ENTRY + 360), NOW, (120,)),
                [],
            )

    def test_isolated_sqlite_claims_prevent_overlapping_three_attempts(self):
        p1, p2 = patched_patterns()
        with p1, p2, tempfile.TemporaryDirectory() as folder:
            store = EarlyJournal(Path(folder) / "early.db")
            rows = candidate_rows(snapshot(), NOW, (120,))
            accepted = store.record(rows)
            self.assertEqual(len(accepted), 4)
            self.assertEqual(store.record(rows), [])
            moved = [dict(r, entry_at=ENTRY + 120,
                          expires_at=r["expires_at"] + 120,
                          observed_at=NOW + 120) for r in rows]
            self.assertEqual(store.record(moved), [])
            moved2 = [dict(r, entry_at=ENTRY + 360,
                           expires_at=r["expires_at"] + 360,
                           observed_at=NOW + 360) for r in rows]
            self.assertEqual(len(store.record(moved2)), 4)
            with sqlite3.connect(store.path) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM candidates").fetchone()[0], 8)
                self.assertEqual(db.execute("SELECT count(*) FROM early_slots").fetchone()[0], 8)


class EarlyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_observer_is_opt_in_silent_and_persists_outcomes(self):
        p1, p2 = patched_patterns()
        with p1, p2, tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            t = [NOW]
            shadow = EarlyLeadObserver(root, clock=lambda: t[0])
            service = SimpleNamespace(running=lambda: True, transport="live")
            cycles = SimpleNamespace(enabled=True, allowed_timeframes=(120,))
            shadow.observe(*snapshot())
            await shadow.tick(service, cycles)
            await shadow.tick(service, cycles)
            self.assertEqual(len(shadow.journal.pending_future(NOW)), 4)
            # A single pair has one common market tick, sampled by the
            # shadow, not by forging independent outcome prices.
            t[0] = ENTRY
            shadow.observe_tick(Tick("A_otc", ENTRY, 100), ENTRY)
            t[0] = ENTRY + 120
            shadow.observe_tick(Tick("A_otc", ENTRY + 120, 102), ENTRY + 120)
            await shadow.flush()
            groups = comparison(root, since=0, now=ENTRY + 121)
            group = next(g for g in groups if g["strategy"] == "S1" and g["seconds"] == 120)
            self.assertEqual(group["early"], 1)
            self.assertEqual(group["early_outcomes"], {"WIN": 1})
            self.assertEqual(group["live"], 0)
            self.assertIn("только фоновые прогнозы", format_report(groups))
            self.assertFalse((root / "data" / "bollinger-cycles.db").exists())
            self.assertFalse((root / "data" / "community-live.db").exists())
            self.assertFalse((root / "data" / "signal-delivery.db").exists())


if __name__ == "__main__":
    unittest.main()
