import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from database.demo_trade_repository import DemoTradeRepository
from database.signal_log_repository import SignalLogRepository
from services.app_settings import AppSettings
from services.statistics_export_service import StatisticsExportService


class StatisticsExportTests(unittest.TestCase):
    def test_builds_excel_workbook_with_review_sheets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.db"
            with patch.dict(os.environ, {"TRADING_AI_DB_PATH": str(path)}):
                trade_repository = DemoTradeRepository()
                trade_repository.save_open_trade({
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry_price": 100,
                    "quantity": 1,
                    "take_profit": 105,
                    "stop_loss": 97,
                })
                SignalLogRepository().save({
                    "symbol": "BTCUSDT",
                    "signal": "🟢 LONG",
                    "score": 55,
                    "final_approved": True,
                    "strategy_reason": "Стратегия подтвердила вход",
                    "multi_tf_match_count": 2,
                    "multi_tf_required": 2,
                    "multi_tf_avg_score": 48,
                    "execution_status": "failed",
                    "execution_error": "symbol unavailable",
                })
                AppSettings().set("quality_score", "60")

                workbook = StatisticsExportService().build_xlsx()

            with zipfile.ZipFile(workbook) as archive:
                names = set(archive.namelist())
                self.assertIn("xl/workbook.xml", names)
                self.assertIn("xl/worksheets/sheet2.xml", names)
                workbook_xml = archive.read("xl/workbook.xml")
                trades_xml = archive.read("xl/worksheets/sheet2.xml")
                summary_xml = archive.read("xl/worksheets/sheet1.xml")
                signals_xml = archive.read("xl/worksheets/sheet3.xml")
                ElementTree.fromstring(workbook_xml)
                ElementTree.fromstring(trades_xml)
                self.assertIn("Сделки".encode(), workbook_xml)
                self.assertIn(b"BTCUSDT", trades_xml)
                self.assertIn(
                    "Ошибок исполнения".encode(),
                    summary_xml,
                )
                self.assertIn(b"execution_status", signals_xml)
                self.assertIn(b"symbol unavailable", signals_xml)


if __name__ == "__main__":
    unittest.main()
