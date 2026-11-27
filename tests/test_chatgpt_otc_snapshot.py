import sqlite3
import tempfile
import unittest
from pathlib import Path
from scripts.chatgpt_otc_snapshot import snapshot, publish
from unittest.mock import patch

class SnapshotTest(unittest.TestCase):
    def test_closed_fresh_contiguous(self):
        now=12000.0
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"market.db"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE bars(symbol TEXT,time INTEGER,open REAL,high REAL,low REAL,close REAL,last_observed REAL,revisions INTEGER,source TEXT)")
                for t in range(12000-60*65,12001,60):
                    db.execute("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?)",("EURUSD_otc",t,1,2,0.5,1.2,t+60,0,"otcharts_history"))
            out=snapshot(path,now=now)
            self.assertEqual(out["usable_assets"],1)
            self.assertEqual(out["assets"][0]["bars"][-1][0],11940)
            self.assertEqual(snapshot(path,now=now+600)["usable_assets"],0)

    @patch("scripts.chatgpt_otc_snapshot.gh")
    def test_public_upload_denied(self, gh):
        gh.return_value={"private":False,"visibility":"public"}
        with self.assertRaises(ValueError):
            publish({},"owner/repo")
        self.assertEqual(gh.call_count,1)

if __name__=="__main__":
    unittest.main()
