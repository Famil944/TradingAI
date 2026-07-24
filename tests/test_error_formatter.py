import unittest

from binance.error import ClientError

from services.error_formatter import user_error_message


class ErrorFormatterTests(unittest.TestCase):
    def test_hides_binance_headers(self):
        error = ClientError(
            401,
            -2015,
            "Invalid API-key",
            {"secret-looking-header": "value"},
        )
        message = user_error_message(error)
        self.assertIn("API-ключ", message)
        self.assertNotIn("secret-looking-header", message)

    def test_ignores_unchanged_telegram_message(self):
        error = RuntimeError("Message is not modified")
        self.assertIsNone(user_error_message(error))
