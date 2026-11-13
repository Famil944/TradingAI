import tempfile
import unittest
from pathlib import Path

from services.otc_candidate_research import CandidateJournal
from services.otc_m1_report import format_report, report, VERSION
from services.otc_market_archive import MarketArchive
from services.otc_strategy import Candle


class M1ReportTests(unittest.TestCase):
    def test_paired_comparison_includes_rejected_baseline_wins(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            journal = CandidateJournal(path / "ready-candidates.db")
            archive = MarketArchive(path / "market-history.db")
            entry = 1800000060
            for symbol, allowed in (("A", True), ("B", False)):
                journal.record([dict(
                    version=VERSION, symbol=symbol,
                    entry_at=entry, expires_at=entry + 60,
                    observed_at=entry - 17, direction="UP", payout=90,
                    m1_baseline_eligible=True, m1_strict_eligible=allowed,
                    reason="eligible" if allowed else "m1_filter_closed_m5_trend",
                    gates={"m1_closed_m5_trend": allowed},
                )])
                archive.bars(symbol, [
                    Candle(entry, 1, 1.001, 0.999, 1.0),
                    Candle(entry + 60, 1.002, 1.003, 1.001, 1.002),
                ], entry + 180)
            result = report(path, since=entry - 60, now=entry + 240)
            self.assertEqual(result["baseline"]["wins"], 2)
            self.assertEqual(result["strict"]["wins"], 1)
            self.assertEqual(result["rejected"]["wins"], 1)
            self.assertEqual(result["pair_n"], 2)
            self.assertAlmostEqual(result["paired_delta_per_opportunity"], -0.45)
            self.assertIn("Pocket Option", format_report(result))
            self.assertFalse(result["advantage_confirmed"])

    def test_missing_archive_remains_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            CandidateJournal(path / "ready-candidates.db").record([dict(
                version=VERSION, symbol="A", entry_at=1800000060,
                expires_at=1800000120, observed_at=1800000042,
                direction="UP", payout=92, m1_baseline_eligible=True,
                m1_strict_eligible=False, reason="m1_stale_tick",
            )])
            result = report(path, since=1800000000, now=1800000900)
            self.assertEqual(result["baseline"]["unknown"], 1)
            self.assertEqual(result["pair_n"], 0)

    def test_empty_no_mutations(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(report(temp)["status"], "no_data")
