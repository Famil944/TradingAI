import tempfile
import unittest
from pathlib import Path

from database.db import Database
from services.otc_conditional import Setup, make_plan


class SerialExpirationTests(unittest.TestCase):
    def test_cross_instrument_waits_for_expiry_and_assessment(self):
        for seconds in (300, 900):
            with self.subTest(seconds=seconds), tempfile.TemporaryDirectory() as folder:
                db = Database(Path(folder) / "bot.db")
                db.init_db()
                at = 1800000000
                setup = Setup("UP", "red", 0.5, "fixture")
                first = make_plan("A", at, setup, at, seconds=seconds)
                identifier = db.prepare_plan(first, at)
                db.plan_delivered(identifier, at, True)
                later = at + 180
                second = make_plan("B", later, setup, later, seconds=seconds)
                self.assertIsNone(db.prepare_plan(second, later))
                # Other expirations stay independent.
                other_seconds = 900 if seconds == 300 else 300
                other = make_plan("C", later, setup, later, seconds=other_seconds)
                self.assertIsNotNone(db.prepare_plan(other, later))
                after = first.expires_at + 60
                candidate = make_plan("B", after, setup, after, seconds=seconds)
                self.assertIsNone(db.prepare_plan(candidate, after))
                db.assess_plan(
                    identifier,
                    dict(outcome="WIN", condition="MET", reason="fixture"),
                    after,
                )
                self.assertIsNotNone(db.prepare_plan(candidate, after))

    def test_failed_delivery_does_not_block_other_instrument(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "bot.db")
            db.init_db()
            at = 1800000000
            setup = Setup("UP", "red", 0.5, "fixture")
            first = make_plan("A", at, setup, at, seconds=300)
            identifier = db.prepare_plan(first, at)
            db.plan_delivered(identifier, at, False)
            at += 180
            self.assertIsNotNone(
                db.prepare_plan(make_plan("B", at, setup, at, seconds=300), at)
            )
