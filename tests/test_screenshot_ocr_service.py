import unittest

from services.screenshot_ocr_service import parse_binance_screenshot_text


class ScreenshotOcrTests(unittest.TestCase):
    def test_parses_binance_position_text(self):
        result = parse_binance_screenshot_text("""
AAVE
0,076923
Себестоимость
$128,72
Нереализованный PnL +0,07999992 USDT
Покупка +0,077
27 авг. 2026 г. 13:04:07
Торговая комиссия -0,000077
""")
        self.assertEqual(result.entry_price, 128.72)
        self.assertEqual(result.quantity, 0.076923)
        self.assertEqual(result.opened_at, "2026-08-27 13:04:07")


if __name__ == "__main__":
    unittest.main()
