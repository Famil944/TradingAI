import tempfile
import unittest
from pathlib import Path

from services.otc_candidate_research import CandidateJournal
from services.otc_filter_report import format_report, report
from services.otc_market_archive import MarketArchive
from services.otc_strategy import Candle


class FilterReportTests(unittest.TestCase):
    def test_accepted_and_rejected_opportunities_remain_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            journal = CandidateJournal(data / "ready-candidates.db")
            archive = MarketArchive(data / "market-history.db")
            entry = 1800000060
            for symbol, reason, allowed in (
                ("A", "eligible", True),
                ("B", "filter_not_doji", False),
            ):
                journal.record([dict(
                    version="primary-ready-v7-confirmation-gate", symbol=symbol,
                    entry_at=entry, expires_at=entry + 60,
                    observed_at=entry - 18, direction="UP", payout=90,
                    regime="uptrend", strategy="trend_continuation",
                    gates={"not_doji": allowed, "min_confirmations": True},
                    reason=reason,
                )])
                archive.bars(symbol, [
                    Candle(entry, 1, 1.1, 0.9, 1),
                    Candle(entry + 60, 1.01, 1.1, 0.9, 1.01),
                ], entry + 200)
            result = report(data, since=entry - 30, until=entry + 250)
            self.assertEqual(result["candidates"], 2)
            self.assertEqual(result["by_policy"]["eligible"]["wins"], 1)
            self.assertEqual(result["by_policy"]["rejected"]["wins"], 1)
            self.assertEqual(result["rejected_filters"]["not_doji"]["opportunities"], 1)
            self.assertIn("Pocket Option", format_report(result))

    def test_empty_data_has_no_implicit_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(report(folder)["status"], "no_data")
