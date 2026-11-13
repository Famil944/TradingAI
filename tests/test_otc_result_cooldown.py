import tempfile
import unittest
from pathlib import Path

from services.otc_ml.preview import PreviewJournal
from services.otc_ready import best_expirations


def signal(seconds=60, at=1200, symbol="A", count=3):
    return dict(
        symbol=symbol,
        direction="UP",
        entry_at=at,
        expires_at=at + seconds,
        payout=90,
        model_sha256="fixture",
        confirmations=dict(count=count),
    )


class CooldownTests(unittest.TestCase):
    def test_result_not_expiry_starts_pause_persistent_and_per_source(self):
        with tempfile.TemporaryDirectory() as folder:
            first = PreviewJournal(Path(folder) / "first.db")
            other = PreviewJournal(Path(folder) / "other.db")
            identifier = first.reserve(signal(), 1180)
            first.sent(identifier, 1180)
            self.assertEqual(first.blocked(2000), "pending_position")
            first.finish(identifier, "WIN", 1, 2, resolved_at=2000)
            first = PreviewJournal(first.path)
            self.assertEqual(first.blocked(2059.99), "result_cooldown")
            self.assertIsNone(first.reserve(signal(at=2100), 2059))
            self.assertIsNone(other.blocked(2059))
            self.assertIsNotNone(other.reserve(signal(at=2100), 2059))
            self.assertIsNone(first.blocked(2060))
            self.assertIsNotNone(first.reserve(signal(at=2100), 2060))

    def test_primary_horizons_independent_and_failed_delivery_has_no_pause(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = PreviewJournal(Path(folder) / "primary.db", per_horizon=True)
            identifier = journal.reserve(signal(300), 1180)
            journal.sent(identifier, 1180)
            journal.finish(identifier, "LOSS", 2, 1, resolved_at=1600)
            self.assertEqual(journal.blocked(1610, 300), "result_cooldown")
            self.assertIsNone(journal.blocked(1610, 60))
            one = journal.reserve(signal(60, at=1680), 1610)
            journal.finish(one, "NOT_DELIVERED", resolved_at=1610)
            self.assertIsNone(journal.blocked(1611, 60))
            # Duplicate callbacks cannot move the result timestamp forward.
            journal.finish(identifier, "LOSS", 2, 1, resolved_at=1700)
            self.assertIsNone(journal.blocked(1660, 300))

    def test_best_timeframe_per_asset_uses_votes_then_shorter_duration(self):
        candidates = [
            signal(60, count=2),
            signal(300, count=4),
            signal(900, count=4),
            signal(60, symbol="B", count=3),
        ]
        chosen = best_expirations(candidates)
        self.assertEqual(len(chosen), 2)
        self.assertEqual(chosen[0]["expires_at"] - chosen[0]["entry_at"], 300)
        self.assertEqual(chosen[1]["symbol"], "B")
