"""Regressions for the safe signal and collector gates."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config.settings import Settings
from services.otc_data import AccessUnavailable
from services.otc_market_archive import MarketArchive
from services.otc_ml.collector import run_once
from services.otc_session import OTCSession


class ReliabilitySettingsTests(unittest.TestCase):
    def test_confirmation_count_is_bounded(self):
        with self.assertRaises(ValueError):
            Settings(ready_min_confirmations=1)
        with self.assertRaises(ValueError):
            Settings(ready_min_confirmations=6)
        self.assertEqual(Settings(ready_min_confirmations=3).ready_min_confirmations, 3)


class CollectorCostTests(unittest.TestCase):
    def test_no_full_archive_export_when_insufficient_new_bars(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = MarketArchive(Path(folder) / "market.db")
            with patch.object(archive, "export", side_effect=AssertionError("export called")):
                result = run_once(archive, Path(folder) / "ml")
            self.assertEqual(result, {"status": "collecting", "rows": 0})


class ReadySnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_mode_rejects_rest_only_fallback(self):
        config = SimpleNamespace(otcharts_api_key="", primary_mode="ready", test_mode=True)
        service = OTCSession(db=None, provider=object(), config=config)
        with self.assertRaisesRegex(AccessUnavailable, "live"):
            await service._poll(AsyncMock(), SimpleNamespace(remaining=100))
