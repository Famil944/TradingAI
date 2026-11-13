import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services.otc_delivery_guard import claim


class DeliveryGuardTests(unittest.TestCase):
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
            self.assertTrue(claim(path, dict(signal, expires_at=900), "Primary"))
            self.assertTrue(
                claim(path, dict(signal, entry_at=660, expires_at=720), "Primary")
            )
