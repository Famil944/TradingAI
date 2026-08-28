import json
import unittest

from services.diagnostic_export_service import build_diagnostic_json


class DiagnosticExportTests(unittest.TestCase):
    def test_report_contains_scan_and_no_secrets(self):
        content = build_diagnostic_json(
            {"checked": 1, "symbols": [{"symbol": "BTCUSDT", "reason": "signal"}]},
            [{"id": 1, "symbol": "BTCUSDT", "status": "open", "entry_price": 100}],
        )
        report = json.loads(content)
        self.assertEqual(report["last_scan"]["checked"], 1)
        self.assertEqual(report["trades"][0]["symbol"], "BTCUSDT")
        raw = content.decode().lower()
        self.assertNotIn("telegram_bot_token", raw)
        self.assertNotIn("cryptopanic_auth_token", raw)


if __name__ == "__main__":
    unittest.main()
