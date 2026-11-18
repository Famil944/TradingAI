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

    def test_global_three_attempt_timeframe_serializes_all_symbols_and_strategies(self):
        for seconds in (120, 300, 900):
            with self.subTest(seconds=seconds), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "guard.db"
                start = 1800
                length = seconds * 3
                first = dict(
                    symbol="Tesla OTC", direction="DOWN", entry_at=start,
                    expires_at=start + length,
                )
                self.assertTrue(claim(
                    path, first, "S4", start - 10, exclusive_timeframe=True,
                ))
                # Second publisher, different pair and direction, same M period:
                # block even if it would start on the second or third attempt.
                for shift in (0, seconds, 2 * seconds):
                    other = dict(
                        symbol="Microsoft OTC", direction="UP",
                        entry_at=start + shift, expires_at=start + shift + length,
                    )
                    self.assertFalse(claim(
                        path, other, "S1", start + shift - 10,
                        exclusive_timeframe=True,
                    ))
                # The entry at the exact end of attempt 3 is allowed.
                next_signal = dict(
                    symbol="Microsoft OTC", direction="UP",
                    entry_at=start + length, expires_at=start + length * 2,
                )
                self.assertTrue(claim(
                    path, next_signal, "S2", start + length - 10,
                    exclusive_timeframe=True,
                ))

    def test_shorter_m2_and_longer_m5_m15_are_separate_locks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.db"
            start = 1800
            for minutes, symbol, source in ((2, "Tesla", "S1"),
                                            (5, "Microsoft", "S2"),
                                            (15, "Facebook", "S4")):
                self.assertTrue(claim(
                    path,
                    dict(symbol=symbol, direction="UP", entry_at=start,
                         expires_at=start + 3 * minutes * 60),
                    source, start - 10, exclusive_timeframe=True,
                ))
            self.assertFalse(claim(
                path, dict(symbol="CADCHF", direction="DOWN", entry_at=start + 120,
                           expires_at=start + 480), "S3", start + 115,
                exclusive_timeframe=True,
            ))

    def test_early_terminal_result_does_not_unlock_other_pairs_before_three_attempts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.db"
            start = 1800
            first = dict(symbol="Tesla", direction="DOWN",
                         entry_at=start, expires_at=start + 360)
            self.assertTrue(claim(
                path, first, "S4", start - 10, exclusive_timeframe=True,
            ))
            self.assertTrue(completed(path, first, "S4", start + 120))
            # Local same-pair lock may release early, but global M2 lock persists.
            self.assertFalse(claim(
                path,
                dict(symbol="Facebook", direction="UP",
                     entry_at=start + 120, expires_at=start + 480),
                "S1", start + 110, exclusive_timeframe=True,
            ))
            self.assertTrue(claim(
                path,
                dict(symbol="Facebook", direction="UP",
                     entry_at=start + 360, expires_at=start + 720),
                "S1", start + 350, exclusive_timeframe=True,
            ))

    def test_global_claim_is_atomic_between_concurrent_strategies(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.db"
            start = 1800
            with ThreadPoolExecutor(max_workers=4) as pool:
                outcomes = list(pool.map(
                    lambda index: claim(
                        path,
                        dict(symbol=f"PAIR-{index}", direction="UP",
                             entry_at=start, expires_at=start + 900),
                        f"S{index+1}", start - 10, exclusive_timeframe=True,
                    ),
                    range(4),
                ))
            self.assertEqual(sum(outcomes), 1)

    def test_existing_claim_from_previous_version_blocks_global_overlap(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.db"
            # Old databases store exactly the same three-attempt duration.
            self.assertTrue(claim(
                path, dict(symbol="Tesla", direction="UP", entry_at=1800,
                           expires_at=2700), "old", 1790,
            ))
            self.assertFalse(claim(
                path, dict(symbol="Other", direction="DOWN", entry_at=2100,
                           expires_at=3000), "S4", 2090,
                exclusive_timeframe=True,
            ))

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
