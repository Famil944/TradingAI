from contextlib import closing
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services.otc_delivery_guard import blocking_claim, claim, completed


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

    def test_one_strategy_one_timeframe_blocks_own_three_attempts(self):
        for seconds in (120, 300, 900):
            with self.subTest(seconds=seconds), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "guard.db"
                start = 1800
                duration = seconds * 3
                s1 = dict(symbol="Tesla OTC", direction="DOWN", entry_at=start,
                          expires_at=start + duration)
                self.assertTrue(claim(
                    path, s1, "S1", start - 10, exclusive_timeframe=True,
                ))
                for shift in (0, seconds, 2 * seconds):
                    same_source = dict(
                        symbol="Microsoft OTC", direction="UP",
                        entry_at=start + shift,
                        expires_at=start + shift + duration,
                    )
                    self.assertFalse(claim(
                        path, same_source, "S1", start + shift - 10,
                        exclusive_timeframe=True,
                    ))
                self.assertTrue(claim(
                    path,
                    dict(symbol="Microsoft OTC", direction="UP",
                         entry_at=start + duration, expires_at=start + 2 * duration),
                    "S1", start + duration - 10, exclusive_timeframe=True,
                ))

    def test_s1_does_not_block_s2_s3_s4_even_on_same_symbol_and_minute(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.db"
            s = dict(symbol="Tesla OTC", direction="DOWN", entry_at=1800,
                     expires_at=2160)
            for source in ("S1", "S2", "S3", "S4"):
                self.assertTrue(claim(
                    path, s, source, 1790, exclusive_timeframe=True,
                ))
            for source in ("S1", "S2", "S3", "S4"):
                self.assertFalse(claim(
                    path, dict(s, symbol="New pair", entry_at=1920, expires_at=2280),
                    source, 1910, exclusive_timeframe=True,
                ))

    def test_m2_m5_m15_within_same_strategy_are_independent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.db"
            for seconds in (120, 300, 900):
                self.assertTrue(claim(
                    path,
                    dict(symbol="Tesla OTC", direction="UP",
                         entry_at=1800, expires_at=1800 + seconds * 3),
                    "S1", 1790, exclusive_timeframe=True,
                ))
            # Other strategies' same-minute and same-expiry signals are independent.
            self.assertTrue(claim(
                path,
                dict(symbol="Tesla OTC", direction="DOWN",
                     entry_at=1800, expires_at=2160),
                "S4", 1790, exclusive_timeframe=True,
            ))

    def test_early_terminal_result_does_not_unlock_own_timeframe(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.db"
            first = dict(symbol="Tesla", direction="DOWN",
                         entry_at=1800, expires_at=2160)
            self.assertTrue(claim(
                path, first, "S4", 1790, exclusive_timeframe=True,
            ))
            # completed() can finish a legacy symbol lock but must not shorten
            # the new scoped three-attempt reservation.
            completed(path, first, "S4", 1920)
            self.assertFalse(claim(
                path,
                dict(symbol="Facebook", direction="UP",
                     entry_at=1920, expires_at=2280),
                "S4", 1910, exclusive_timeframe=True,
            ))
            self.assertTrue(claim(
                path,
                dict(symbol="Facebook", direction="UP",
                     entry_at=1920, expires_at=2280),
                "S1", 1910, exclusive_timeframe=True,
            ))
            self.assertTrue(claim(
                path,
                dict(symbol="Facebook", direction="UP",
                     entry_at=2160, expires_at=2520),
                "S4", 2150, exclusive_timeframe=True,
            ))

    def test_scoped_claim_is_atomic_between_concurrent_same_strategy_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.db"
            with ThreadPoolExecutor(max_workers=4) as pool:
                outcomes = list(pool.map(
                    lambda index: claim(
                        path,
                        dict(symbol=f"PAIR-{index}", direction="UP",
                             entry_at=1800, expires_at=2700),
                        "S1", 1790, exclusive_timeframe=True,
                    ),
                    range(4),
                ))
            self.assertEqual(sum(outcomes), 1)

    def test_previous_version_claims_only_block_matching_source_and_timeframe(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.db"
            self.assertTrue(claim(
                path, dict(symbol="Tesla", direction="UP", entry_at=1800,
                           expires_at=2700), "Боллинджер ×3", 1790,
            ))
            # Old live S1 claim still blocks S1 M5 in a new process.
            self.assertFalse(claim(
                path, dict(symbol="Other", direction="DOWN", entry_at=2100,
                           expires_at=3000), "Боллинджер ×3", 2090,
                exclusive_timeframe=True,
            ))
            # But never S2 M5, S1 M2, or S3 on the same old symbol.
            self.assertTrue(claim(
                path, dict(symbol="Other", direction="DOWN", entry_at=2100,
                           expires_at=3000), "bb_reentry", 2090,
                exclusive_timeframe=True,
            ))
            self.assertTrue(claim(
                path, dict(symbol="Tesla", direction="DOWN", entry_at=2100,
                           expires_at=2460), "Боллинджер ×3", 2090,
                exclusive_timeframe=True,
            ))

    def test_overlap_diagnostics_show_which_S4_M15_reservation_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.db"
            first = dict(symbol="AUDCAD_otc", direction="DOWN",
                         entry_at=1800, expires_at=4500)
            self.assertTrue(claim(
                path, first, "ema_pullback", 1790, exclusive_timeframe=True,
            ))
            second = dict(symbol="AUDCHF_otc", direction="UP",
                          entry_at=2400, expires_at=5100)
            self.assertFalse(claim(
                path, second, "ema_pullback", 2390, exclusive_timeframe=True,
            ))
            blocker = blocking_claim(path, second, "ema_pullback")
            self.assertEqual(blocker, dict(
                symbol="AUDCAD_otc", source="ema_pullback",
                entry_at=1800, expires_at=4500, origin="scoped",
            ))
            # S2 M15 and S4 M2 are independent of the S4 M15 lock.
            self.assertIsNone(blocking_claim(path, second, "bb_reentry"))
            m2 = dict(second, expires_at=2760)
            self.assertIsNone(blocking_claim(path, m2, "ema_pullback"))
            after = dict(second, entry_at=4500, expires_at=7200)
            self.assertIsNone(blocking_claim(path, after, "ema_pullback"))
            self.assertTrue(claim(
                path, after, "ema_pullback", 4490, exclusive_timeframe=True,
            ))

    def test_overlap_diagnostics_support_legacy_source_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.db"
            first = dict(symbol="Tesla", direction="UP",
                         entry_at=1800, expires_at=2160)
            self.assertTrue(claim(path, first, "ema_pullback", 1790))
            blocked = dict(symbol="Other", direction="DOWN",
                           entry_at=1920, expires_at=2280)
            self.assertFalse(claim(
                path, blocked, "ema_pullback", 1910, exclusive_timeframe=True,
            ))
            self.assertEqual(
                blocking_claim(path, blocked, "ema_pullback")["origin"],
                "legacy",
            )
            self.assertIsNone(blocking_claim(path, blocked, "break_retest"))

    def test_additive_migration_of_existing_claims(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.db"
            with closing(sqlite3.connect(path)) as db, db:
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
