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
        self.assertEqual(report["report_version"], 3)
        self.assertEqual(report["scan_quality"]["data_availability_percent"], 100.0)
        self.assertEqual(report["trades"][0]["target_price"], 103)
        raw = content.decode().lower()
        self.assertNotIn("telegram_bot_token", raw)
        self.assertNotIn("cryptopanic_auth_token", raw)

    def test_report_distinguishes_api_failure_from_strategy_rejection(self):
        content = build_diagnostic_json(
            {
                "checked": 10,
                "accepted": 0,
                "symbols": [
                    {"symbol": f"X{index}USDT", "reason": "market_data_unavailable"}
                    for index in range(3)
                ] + [
                    {"symbol": f"Y{index}USDT", "reason": "weak_volume"}
                    for index in range(7)
                ],
            },
            [{"id": 1, "status": "pending_close", "entry_price": 1,
              "current_price": 1.03, "pending_reason": "TP +3%"}],
        )
        report = json.loads(content)
        self.assertEqual(report["scan_quality"]["operational_failures"], 3)
        self.assertEqual(report["scan_quality"]["strategy_rejections"], 7)
        self.assertEqual(report["scan_quality"]["data_availability_percent"], 70.0)
        codes = {item["code"] for item in report["recommendations"]}
        self.assertIn("unstable_market_data", codes)
        self.assertIn("pending_trade_confirmation", codes)


if __name__ == "__main__":
    unittest.main()
