import tempfile
import unittest
from pathlib import Path

from services.otc_candidate_research import CandidateJournal, TickSampler
from services.otc_shadow_report import cycle_outcome, format_active_rates, report
from services.otc_shadow_variants import VERSION


class CycleTests(unittest.TestCase):
    def test_any_win_three_losses_and_missing_data(self):
        self.assertEqual(cycle_outcome(["LOSS", "WIN", "PENDING"]), "WIN")
        self.assertEqual(cycle_outcome(["UNKNOWN", "LOSS", "WIN"]), "WIN")
        self.assertEqual(cycle_outcome(["LOSS"] * 3), "LOSS")
        self.assertEqual(cycle_outcome(["LOSS", "UNKNOWN", "LOSS"]), "UNKNOWN")
        self.assertEqual(cycle_outcome(["LOSS", "DRAW", "LOSS"]), "DRAW")
        self.assertEqual(cycle_outcome(["LOSS", "PENDING", "PENDING"]), "PENDING")

    def test_cycle_samples_all_endpoints_and_no_overlap_after_restart(self):
        row = dict(
            version="bollinger-three-attempts-v1",
            symbol="A",
            entry_at=1800,
            expires_at=2700,
            observed_at=1780,
            direction="UP",
            gates={"min_confirmation": True},
            experiment=dict(version=VERSION, masks={}, cycle_seconds=300),
        )
        sampler = TickSampler()
        sampler.register([row], 1780)
        self.assertTrue({1800, 2100, 2400, 2700} <= sampler.targets["A"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "research.db"
            journal = CandidateJournal(path)
            journal.record([row])
            CandidateJournal(path).record(
                [dict(row, entry_at=2100, expires_at=3000, observed_at=2080)]
            )
            journal.samples(
                [
                    ("A", t, t, t, p)
                    for t, p in [(1800, 100), (2100, 99), (2400, 98), (2700, 99)]
                ]
            )
            data = report(path, 0, 2800)
            self.assertEqual(data["candidates"], 1)
            self.assertEqual(data["cycles"][0]["counts"], {"WIN": 1})
            self.assertFalse(data["filters"])
            self.assertIn("WR 100.0%", format_active_rates(data, 300))
            self.assertEqual(
                report(path, 0, 2200)["cycles"][0]["counts"], {"PENDING": 1}
            )
            self.assertEqual(report(path, 1790, 2800)["candidates"], 0)
