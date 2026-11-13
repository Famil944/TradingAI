import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from services.otc_reconcile import reconcile


class ReconcileTests(unittest.TestCase):
    def test_exact_recovery_audit_backup_and_idempotence(self):
        with tempfile.TemporaryDirectory() as folder:
            journal, archive = Path(folder) / "journal.db", Path(folder) / "archive.db"
            with closing(sqlite3.connect(journal)) as db:
                db.execute(
                    "CREATE TABLE predictions(id,symbol,entry_at,expires_at,sent_at,direction,outcome,entry_price,expiry_price,evidence)"
                )
                for identifier, symbol, direction, sent in (
                    ("win", "A", "UP", 580),
                    ("loss", "A", "DOWN", 580),
                    ("revised", "B", "UP", 580),
                    ("late", "A", "UP", 599),
                ):
                    db.execute(
                        "INSERT INTO predictions VALUES(?,?,?,?,?,?,?,NULL,NULL,?)",
                        (
                            identifier,
                            symbol,
                            600,
                            660,
                            sent,
                            direction,
                            "UNKNOWN",
                            "{}",
                        ),
                    )
                db.commit()
            with closing(sqlite3.connect(archive)) as db:
                db.execute(
                    "CREATE TABLE bars(symbol,time,open,source,revisions,last_observed)"
                )
                db.executemany(
                    "INSERT INTO bars VALUES(?,?,?,?,?,?)",
                    [
                        ("A", 600, 1, "otcharts_history", 0, 670),
                        ("A", 660, 2, "otcharts_history", 0, 730),
                        ("B", 600, 1, "otcharts_history", 0, 670),
                        ("B", 660, 2, "otcharts_history", 1, 730),
                    ],
                )
                db.commit()
            self.assertEqual(
                sum(
                    r["status"] == "recoverable"
                    for r in reconcile(journal, archive, now=800)
                ),
                2,
            )
            self.assertFalse(list(Path(folder).glob("*.backup-*")))
            self.assertEqual(
                sum(
                    r["status"] == "recovered"
                    for r in reconcile(journal, archive, now=800, apply=True)
                ),
                2,
            )
            self.assertEqual(len(list(Path(folder).glob("*.backup-*"))), 1)
            with closing(sqlite3.connect(journal)) as db:
                rows = {
                    r[0]: r[1:]
                    for r in db.execute("SELECT id,outcome,evidence FROM predictions")
                }
            self.assertEqual(rows["win"][0], "WIN")
            self.assertEqual(rows["loss"][0], "LOSS")
            self.assertEqual(rows["revised"][0], "UNKNOWN")
            self.assertEqual(rows["late"][0], "UNKNOWN")
            self.assertEqual(
                json.loads(rows["win"][1])["reconciliation"]["previous_outcome"],
                "UNKNOWN",
            )
            self.assertFalse(
                any(
                    r["status"] == "recovered"
                    for r in reconcile(journal, archive, now=800, apply=True)
                )
            )
