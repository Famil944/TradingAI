import unittest

from exchange.order_validation import validate_order


class OrderValidationTests(unittest.TestCase):
    def test_normalizes_valid_order(self):
        self.assertEqual(
            validate_order("btcusdt", "buy", 0.001),
            ("BTCUSDT", "BUY", 0.001),
        )

    def test_rejects_invalid_values(self):
        for args in [
            ("BTC/USDT", "BUY", 1),
            ("BTCUSDT", "HOLD", 1),
            ("BTCUSDT", "BUY", 0),
            ("BTCUSDT", "BUY", float("nan")),
        ]:
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    validate_order(*args)
