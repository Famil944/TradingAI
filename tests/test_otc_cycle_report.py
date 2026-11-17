import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from services.otc_cycle_report import report
from services.otc_report_sync import export


class CycleReportTests(unittest.TestCase):
    def test_delivered_cycles_and_shadow_are_exported_without_raw_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "data"
            data.mkdir()
            with closing(sqlite3.connect(data / "bollinger-cycles.db")) as db:
                db.execute("CREATE TABLE predictions(evidence,outcome,sent_at)")
                evidence = json.dumps(
                    dict(
                        cycle_seconds=300,
                        attempt_outcomes=["LOSS", "WIN", "PENDING"],
                        secret="private-value",
                    )
                )
                db.executemany(
                    "INSERT INTO predictions VALUES(?,?,?)",
                    [
                        (evidence, "WIN", 100),
                        (evidence, "UNKNOWN", 110),
                        (evidence, "NOT_DELIVERED", 120),
                        (evidence, "WIN", None),
                        (evidence, "LOSS", 20),
                    ],
                )
                db.commit()
            result = report(data, 50, 200)
            m5 = result["main"][1]
            self.assertEqual(m5["total"], 2)
            self.assertEqual(m5["resolved"], 1)
            self.assertEqual(m5["counts"]["stage2"], 1)
            self.assertEqual(m5["win_rate"], 1)
            self.assertEqual(len(result["shadow"]), 9)
            public = export(root, now=200)
            self.assertIn("cycles", public)
            self.assertNotIn("private-value", json.dumps(public))
            self.assertFalse((data / "community-cycles.db").exists())
