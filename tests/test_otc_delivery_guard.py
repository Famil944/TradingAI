import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services.otc_delivery_guard import claim, completed


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
            self.assertFalse(claim(path, dict(signal, expires_at=900), "Primary"))
            self.assertFalse(claim(path, dict(signal, direction="DOWN"), "Groq"))
            self.assertTrue(
                claim(path, dict(signal, entry_at=660, expires_at=720), "Primary")
            )

    def test_overlapping_three_attempt_cycles_are_blocked_across_durations(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.db"
            first = dict(symbol="#FB_otc", direction="UP", entry_at=600, expires_at=1500)
            self.assertTrue(claim(path, first, "S1", 590))
            # A five-minute SELL on the same instrument is not a new entry.
            second = dict(symbol="#FB_otc", direction="DOWN", entry_at=600, expires_at=1500)
            self.assertFalse(claim(path, second, "S2", 591))
            # Even a later entry is incompatible with outstanding coverage.
            self.assertFalse(claim(
                path, dict(second, entry_at=900, expires_at=1800), "S3", 895
            ))
            # A different instrument is unaffected.
            self.assertTrue(claim(
                path, dict(second, symbol="A", entry_at=900, expires_at=1800),
                "S4", 895,
            ))
            self.assertTrue(claim(
                path, dict(second, entry_at=1500, expires_at=1800), "S2", 1490
            ))

    def test_confirmed_early_finish_unblocks_next_entry_but_not_same_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.db"
            signal = dict(symbol="A", direction="UP", entry_at=600, expires_at=1500)
            self.assertTrue(claim(path, signal, "S1", 590))
            self.assertFalse(claim(
                path, dict(signal, entry_at=900, expires_at=1800), "S2", 895
            ))
            self.assertTrue(completed(path, signal, "S1", 900))
            self.assertFalse(claim(path, dict(signal, direction="DOWN"), "S2", 591))
            self.assertTrue(claim(
                path, dict(signal, entry_at=900, expires_at=1800), "S2", 895
            ))
            self.assertFalse(completed(path, signal, "other", 900))
            self.assertFalse(completed(path, signal, "S1", 1800))

    def test_additive_migration_of_existing_claims(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.db"
            with sqlite3.connect(path) as db:
                db.execute("""CREATE TABLE claims(
                    symbol TEXT, direction TEXT, entry_at INTEGER, expires_at INTEGER,
                    source TEXT, claimed_at REAL,
                    PRIMARY KEY(symbol,direction,entry_at,expires_at))""")
                db.execute("INSERT INTO claims VALUES('A','UP',600,1500,'S1',590)")
            self.assertFalse(claim(
                path, dict(symbol="A", direction="DOWN", entry_at=900,
                           expires_at=1800), "S2", 895
            ))
            self.assertTrue(completed(
                path, dict(symbol="A", entry_at=600), "S1", 900
            ))
            self.assertTrue(claim(
                path, dict(symbol="A", direction="DOWN", entry_at=900,
                           expires_at=1800), "S2", 895
            ))
