import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.app_settings import AppSettings
from auto.auto_state import AutoState


class AppSettingsTests(unittest.TestCase):
    def test_defaults_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.db"
            with patch.dict(os.environ, {"TRADING_AI_DB_PATH": str(path)}):
                settings = AppSettings()
                self.assertEqual(settings.get("timeframe"), "1h")
                settings.set("timeframe", "4h")
                self.assertEqual(AppSettings().get("timeframe"), "4h")

    def test_rejects_unsafe_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.db"
            with patch.dict(os.environ, {"TRADING_AI_DB_PATH": str(path)}):
                settings = AppSettings()
                with self.assertRaises(ValueError):
                    settings.set("risk_percent", "10")

    def test_auto_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.db"
            with patch.dict(os.environ, {"TRADING_AI_DB_PATH": str(path)}):
                state = AutoState()
                state.turn_on()
                self.assertTrue(AutoState().enabled)
                state.turn_off()
                self.assertFalse(AutoState().enabled)
