import unittest
from io import BytesIO
from zipfile import ZipFile

from services.pump_export_service import build_pump_xlsx


class PumpExportTests(unittest.TestCase):
    def test_export_contains_summary_predictions_and_checkpoints(self):
        content = build_pump_xlsx([{
            "id": 7, "symbol": "TESTUSDT", "score": 81,
            "stage": "confirmed", "status": "completed", "outcome": "pump",
            "start_price": 100, "current_price": 104, "max_price": 106,
            "min_price": 98, "news_score": 5, "news_items": 2,
            "news_critical": 0, "technical_json": '{"reasons":["volume"]}',
            "checkpoints_json": '{"5":1.5,"60":4.0}',
            "detected_at": "2026-08-30 10:00:00", "completed_at": "2026-08-31 10:00:00",
        }])
        with ZipFile(BytesIO(content)) as archive:
            self.assertIn("xl/worksheets/sheet3.xml", archive.namelist())
            workbook = archive.read("xl/workbook.xml").decode()
            predictions = archive.read("xl/worksheets/sheet2.xml").decode()
            checkpoints = archive.read("xl/worksheets/sheet3.xml").decode()
            self.assertIn("Сводка", workbook)
            self.assertIn("TESTUSDT", predictions)
            self.assertIn(">6.0<", predictions)
            self.assertIn(">1.5<", checkpoints)


if __name__ == "__main__":
    unittest.main()
