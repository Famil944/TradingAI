import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services.otc_delivery_guard import claim, delivery_snapshot
from services.otc_market import Asset


class DeliveryGuardTests(unittest.TestCase):
    def test_price_only_delivery_allows_missing_payout_for_xgboost(self):
        now = 1800000042
        selected = dict(symbol="A", entry_at=1800000060, expires_at=1800000120)
        snapshot = (
            {},
            {"A": Asset("A", True, True, None, None, "A", now)},
            {"A": now},
            None,
        )
        self.assertIsNone(delivery_snapshot(selected, snapshot, now))
        self.assertIsNotNone(
            delivery_snapshot(
                selected, snapshot, now, require_payout=False
            )
        )

    def test_cross_source_race_one_winner_and_persistent_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.db"
            signal = dict(symbol="A", direction="UP", entry_at=600, expires_at=660)
            with ThreadPoolExecutor(max_workers=3) as pool:
                outcomes = list(
                    pool.map(
                        lambda source: claim(path, signal, source),
                        ["Primary", "XGBoost", "Groq"],
                    )
                )
            self.assertEqual(sum(outcomes), 1)
            self.assertFalse(claim(path, signal, "restarted"))
            self.assertFalse(claim(path, dict(signal, expires_at=900), "Primary"))
            self.assertFalse(claim(path, dict(signal, direction="DOWN"), "Groq"))
            self.assertTrue(
                claim(path, dict(signal, entry_at=660, expires_at=720), "Primary")
            )
