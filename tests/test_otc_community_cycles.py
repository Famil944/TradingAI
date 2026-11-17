import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.otc_candidate_research import CandidateJournal
from services.otc_community_cycles import METHODS, prepare, report, text_report
from services.otc_strategy import Candle


class CommunityCyclesTests(unittest.TestCase):
    def test_nine_variants_are_not_live_and_have_three_targets(self):
        now = 10780
        bars = [Candle(10740 - i * 60, 100, 101, 99, 100.5) for i in range(60, 0, -1)]
        asset = SimpleNamespace(open=True, payout=92, observed_at=now)
        decisions = {key: {"direction": "UP"} for key in METHODS}
        with patch("services.otc_community_cycles.signals", return_value=decisions):
            rows = prepare(({"A": bars}, {"A": asset}, {"A": now}, None), now)
        self.assertEqual(len(rows), 9)
        self.assertTrue(all(not r["selected"] for r in rows))
        self.assertEqual(
            {r["experiment"]["cycle_seconds"] for r in rows}, {120, 300, 900}
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
                    ("A", 10920, 10920, 10920, 99),
                    ("A", 11040, 11040, 11040, 101),
                ]
            )
            data = report(path, 0, 11043)
            m2 = [r for r in data if r["seconds"] == 120]
            self.assertTrue(all(r["counts"] == {"WIN": 1, "stage2": 1} for r in m2))
            self.assertIn("② 1", text_report(data, 120))
            self.assertTrue(
                all(r["counts"].get("PENDING") == 1 for r in report(path, 0, 10801))
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

    def test_no_lookahead_or_past_entries(self):
        self.assertEqual(prepare(({}, {}, {}, None), 10790), [])
