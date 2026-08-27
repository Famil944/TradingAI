import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from services.trading_mode_switcher import TradingModeSwitcher


class FakeCurrentClient:
    def __init__(self, positions=None):
        self.positions = positions or []

    def open_positions(self):
        return self.positions


class FakeTargetClient:
    def health_check(self):
        return {"position_mode": "ONE_WAY"}


class TradingModeSwitcherTests(unittest.TestCase):
    def make_switcher(self, path):
        return TradingModeSwitcher(
            env_path=path,
            client_factory=lambda mode: FakeTargetClient(),
        )

    def test_switches_to_demo_and_preserves_other_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "TRADING_MODE=LIVE\n"
                "BINANCE_TESTNET_API_KEY=test-key\n"
                "BINANCE_TESTNET_API_SECRET=test-secret\n"
                "UNRELATED=value\n",
                encoding="utf-8",
            )
            result = self.make_switcher(path).switch(
                "DEMO", FakeCurrentClient()
            )
            content = path.read_text(encoding="utf-8")

            self.assertEqual(result["mode"], "DEMO")
            self.assertIn("TRADING_MODE=DEMO", content)
            self.assertIn("UNRELATED=value", content)

    def test_live_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "BINANCE_LIVE_API_KEY=key\n"
                "BINANCE_LIVE_API_SECRET=secret\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"LIVE_TRADING_CONFIRMATION": ""},
            ):
                with self.assertRaises(PermissionError):
                    self.make_switcher(path).switch(
                        "LIVE", FakeCurrentClient()
                    )

    def test_open_positions_block_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("", encoding="utf-8")
            with self.assertRaises(PermissionError):
                self.make_switcher(path).switch(
                    "DEMO",
                    FakeCurrentClient([{"symbol": "BTCUSDT"}]),
                )

    def test_render_state_file_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            state_path = Path(directory) / "mode"
            env_path.write_text("", encoding="utf-8")
            environment = {
                "TRADING_MODE_STATE_PATH": str(state_path),
                "BINANCE_TESTNET_API_KEY": "key",
                "BINANCE_TESTNET_API_SECRET": "secret",
            }
            with patch.dict(os.environ, environment):
                self.make_switcher(env_path).switch(
                    "DEMO", FakeCurrentClient()
                )
            self.assertEqual(
                state_path.read_text(encoding="utf-8"),
                "DEMO",
            )
