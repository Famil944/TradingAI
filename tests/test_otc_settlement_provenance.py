import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from services.otc_ml.preview import MLPreview, PreviewJournal
from services.otc_strategy import Candle
from services.otc_signal_report import read_journal, summarize


def signal():
    return dict(
        symbol="A", entry_at=600, expires_at=660, direction="UP",
        payout=90, model_sha256="test",
    )


class ProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_origin_saved_for_primary_live_tick(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = PreviewJournal(Path(folder) / "journal.db")
            pk = journal.reserve(signal(), 580)
            journal.sent(pk, 580)
            journal.finish(pk, "WIN", 1, 2, result_source="otcharts_live_tick")
            with sqlite3.connect(journal.path) as db:
                row = db.execute(
                    "SELECT outcome,result_source FROM predictions WHERE id=?", (pk,)
                ).fetchone()
            self.assertEqual(row, ("WIN", "otcharts_live_tick"))
            display = summarize(read_journal(journal, 500, 800))
            self.assertIn("otcharts_live_tick", display)

    async def test_history_method_saved_for_xgboost(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = PreviewJournal(Path(folder) / "preview.db")
            pk = journal.reserve(signal(), 580)
            journal.sent(pk, 580)
            provider = SimpleNamespace(
                usage=AsyncMock(return_value=SimpleNamespace(remaining=99)),
                candles=AsyncMock(return_value=[
                    Candle(600, 1, 1, 1, 1), Candle(660, 2, 2, 2, 2)
                ])
            )
            preview = MLPreview(journal, None, clock=lambda: 720, settlement_provider=provider)
            await preview.settle_pending({})
            row = read_journal(journal, 500, 800)[0]
            self.assertEqual(row["result_source"], "otcharts_history_open")
            self.assertEqual(row["outcome"], "WIN")

    async def test_legacy_outcomes_remain_unattributed(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = PreviewJournal(Path(folder) / "legacy.db")
            pk = journal.reserve(signal(), 580)
            journal.sent(pk, 580)
            journal.finish(pk, "LOSS", 2, 1)
            old = read_journal(journal, 500, 800)[0]
            self.assertIsNone(old["result_source"])
            self.assertIn("legacy_unattributed", summarize([old]))

    async def test_column_migrates_old_preview_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "old.db"
            with sqlite3.connect(file) as db:
                db.execute(
                    "CREATE TABLE predictions(id TEXT PRIMARY KEY, symbol TEXT, "
                    "created_at REAL, entry_at REAL, expires_at REAL, direction TEXT,"
                    "payout REAL, model_sha TEXT, evidence TEXT, sent_at REAL, "
                    "outcome TEXT DEFAULT 'PENDING', entry_price REAL, expiry_price REAL,"
                    "UNIQUE(entry_at))"
                )
            journal = PreviewJournal(file)
            with sqlite3.connect(journal.path) as db:
                columns = {r[1] for r in db.execute("PRAGMA table_info(predictions)")}
            self.assertIn("result_source", columns)
