import json
import unittest

from services.diagnostic_export_service import build_diagnostic_json


class DiagnosticExportTests(unittest.TestCase):
    def test_report_contains_scan_and_no_secrets(self):
        content = build_diagnostic_json(
            {"checked": 1, "symbols": [{"symbol": "BTCUSDT", "reason": "signal"}]},
            [{"id": 1, "symbol": "BTCUSDT", "status": "open", "entry_price": 100,
              "current_price": 102, "max_price": 104, "min_price": 98,
              "tp1": 103}],
            database_id="source123",
        )
        report = json.loads(content)
        self.assertEqual(report["last_scan"]["checked"], 1)
        self.assertEqual(report["trades"][0]["symbol"], "BTCUSDT")
        self.assertEqual(report["filter_summary"]["signal"], 1)
        self.assertAlmostEqual(report["trades"][0]["result_percent"], 2.0)
        self.assertAlmostEqual(report["trades"][0]["max_favorable_percent"], 4.0)
        self.assertAlmostEqual(report["trades"][0]["max_adverse_percent"], -2.0)
        self.assertEqual(report["database_id"], "source123")
        self.assertEqual(report["trades"][0]["target_price"], 103)
        raw = content.decode().lower()
        self.assertNotIn("telegram_bot_token", raw)
        self.assertNotIn("cryptopanic_auth_token", raw)


if __name__ == "__main__":
    unittest.main()
