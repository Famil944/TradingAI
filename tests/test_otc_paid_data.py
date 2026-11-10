import unittest
from unittest.mock import AsyncMock

from services.otc_data import OTChartsProvider
from services.otc_market import eligible


class PaidDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalogue_preserves_metadata_without_inventing_source_timestamp(
        self,
    ):
        provider = OTChartsProvider("unused")
        provider._get = AsyncMock(
            return_value={
                "symbols": [
                    {
                        "symbol": "LOW",
                        "name": "Low OTC",
                        "isOtc": True,
                        "open": True,
                        "payout": 70,
                    },
                    {
                        "symbol": "HIGH",
                        "name": "High OTC",
                        "isOtc": True,
                        "open": True,
                        "payout": 92,
                    },
                    {"symbol": "CLOSED", "isOtc": True, "open": False, "payout": None},
                    {
                        "symbol": "INVALID",
                        "isOtc": True,
                        "open": True,
                        "payout": float("nan"),
                    },
                ]
            }
        )
        assets = await provider.symbols()
        self.assertEqual(assets[1].name, "High OTC")
        self.assertEqual(assets[1].payout, 92)
        self.assertIsNone(assets[1].payout_at)
        self.assertGreater(assets[1].observed_at, 0)
        self.assertIsNone(assets[3].payout)
        self.assertIn("CLOSED", provider.closed_symbols)
        self.assertEqual(eligible(assets, 0, research=True)[0].symbol, "HIGH")
        self.assertNotIn(
            "CLOSED", [a.symbol for a in eligible(assets, 0, research=True)]
        )
        self.assertEqual(eligible(assets, 0), [])
        provider._get.return_value = {
            "symbols": [{"symbol": "CLOSED", "isOtc": True, "open": True, "payout": 90}]
        }
        await provider.symbols()
        self.assertNotIn("CLOSED", provider.closed_symbols)

    async def test_closed_history_marks_asset_closed(self):
        provider = OTChartsProvider("unused")
        provider._get = AsyncMock(return_value={"open": False, "candles": []})
        self.assertEqual(await provider.candles("X", 1800000000), [])
        self.assertIn("X", provider.closed_symbols)
