import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.otc_candidate_research import CandidateJournal
from services.otc_community_cycles import METHODS, prepare, report, text_report
from services.otc_strategy import Candle


class CommunityCyclesTests(unittest.TestCase):
    def test_three_m2_candidates_have_three_targets(self):
        now = 10780
        bars = [Candle(10740 - i * 60, 100, 101, 99, 100.5) for i in range(60, 0, -1)]
        asset = SimpleNamespace(open=True, payout=92, observed_at=now)
        decisions = {key: {"direction": "UP"} for key in METHODS}
        with patch("services.otc_community_cycles.signals", return_value=decisions):
            rows = prepare(({"A": bars}, {"A": asset}, {"A": now}, None), now)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(not r["selected"] for r in rows))
        self.assertEqual(
            {r["experiment"]["cycle_seconds"] for r in rows}, {60}
        )
        self.assertTrue(
            all(
                r["expires_at"] - r["entry_at"] == 3 * r["experiment"]["cycle_seconds"]
                for r in rows
            )
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cycles.db"
            journal = CandidateJournal(path)
            journal.record(rows)
            journal.samples(
                [
                    ("A", 10800, 10800, 10800, 100),
                    ("A", 10860, 10860, 10860, 99),
                    ("A", 10920, 10920, 10920, 101),
                ]
            )
            data = report(path, 0, 10923)
            m2 = [r for r in data if r["seconds"] == 60]
            self.assertTrue(all(r["counts"] == {"WIN": 1, "stage2": 1} for r in m2))
            self.assertIn("② 1", text_report(data, 60))
            self.assertTrue(
                all(r["counts"].get("PENDING") == 1 for r in report(path, 0, 10801) if r["seconds"] == 60)
            )

    def test_history_symbol_missing_from_current_catalogue_is_skipped(self):
        now = 10780
        bars = [
            Candle(10740 - i * 60, 100, 101, 99, 100.5)
            for i in range(60, 0, -1)
        ]
        self.assertEqual(
            prepare(({"AMD_otc": bars}, {}, {"AMD_otc": now}, None), now),
            [],
        )

    def test_timeframe_diagnostics_show_actual_M5_M15_boundaries(self):
        at_quarter = Counter()
        self.assertEqual(prepare(({}, {}, {}, None), 8981, at_quarter), [])
        self.assertEqual(at_quarter["aligned_M1"], 1)
        self.assertEqual(at_quarter["aligned_M5"], 0)
        self.assertEqual(at_quarter["aligned_M15"], 0)
        only_m2 = Counter()
        self.assertEqual(prepare(({}, {}, {}, None), 9041, only_m2), [])
        self.assertEqual(only_m2["aligned_M1"], 1)
        self.assertEqual(only_m2["aligned_M5"], 0)
        self.assertEqual(only_m2["aligned_M15"], 0)

    def test_no_lookahead_or_past_entries(self):
        self.assertEqual(prepare(({}, {}, {}, None), 10790), [])
