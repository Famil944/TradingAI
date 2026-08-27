import unittest
from io import BytesIO
from zipfile import ZipFile

from services.trade_export_service import build_trades_xlsx


class TradeExportTests(unittest.TestCase):
    def test_export_is_valid_xlsx_package_with_trade(self):
        content = build_trades_xlsx([{
            "id": 1, "symbol": "BTCUSDT", "score": 80,
            "status": "closed", "entry_price": 100.0,
            "close_price": 105.0, "close_reason": "manual",
            "tp1": 103, "tp2": 105, "tp3": 108, "tp4": 115,
            "stop_loss": 97, "max_price": 106, "min_price": 99,
            "opened_at": "2026-08-01", "closed_at": "2026-08-02",
        }])
        with ZipFile(BytesIO(content)) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml").decode()
            self.assertIn("BTCUSDT", sheet)
            self.assertIn(">5.0<", sheet)


if __name__ == "__main__":
    unittest.main()
