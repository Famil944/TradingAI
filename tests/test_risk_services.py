import unittest

from services.binance_risk_service import BinanceRiskService
from services.market_regime_service import MarketRegime
from services.risk_scoring_service import RiskScoringService


class RiskServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_monitoring_tag_is_hard_blocked(self):
        service = BinanceRiskService()
        service._products = service._parse_products({
            "data": [{
                "s": "RISKUSDT", "st": "TRADING",
                "tags": ["Monitoring", "Seed"],
            }]
        })
        service._expires_at = None
        service.refresh = self._available

        result = await service.assess("RISKUSDT")

        self.assertTrue(result.available)
        self.assertTrue(result.blocked)
        self.assertEqual(result.risk_code, "MONITORING_TAG")

    @staticmethod
    async def _available(force=False):
        return True

    def test_risk_score_is_separate_from_signal_score(self):
        result = RiskScoringService.calculate(
            listing_days=90,
            quote_volume=5_500_000,
            spread_percent=0.15,
            trend_metrics={
                "daily_ema20": 80, "daily_ema50": 90,
                "listing_decline_percent": 35,
                "distance_from_history_low_percent": 5,
            },
            drawdown_30d=30,
            market_regime=MarketRegime("HIGH_RISK"),
        )
        self.assertEqual(result.level, "HIGH")
        self.assertGreater(result.score, 69)

    async def test_official_delisting_titles_and_usdt_pairs_are_parsed(self):
        payload = {"data": {"articles": [{
            "title": "Binance Will Delist AAA, BBB on 2026-10-01",
        }]}}

        class NoDetailsSession:
            pass

        events = await BinanceRiskService._parse_risk_events(
            NoDetailsSession(), payload
        )
        self.assertEqual(events["AAAUSDT"], "DELISTING")
        self.assertEqual(events["BBBUSDT"], "DELISTING")

    async def test_delisting_blocks_symbol_missing_from_active_catalog(self):
        service = BinanceRiskService()
        service._products = {}
        service._risk_events = {"GONEUSDT": "DELISTING"}
        service.refresh = self._available

        result = await service.assess("GONEUSDT")

        self.assertTrue(result.available)
        self.assertTrue(result.blocked)
        self.assertEqual(result.risk_code, "DELISTING")


if __name__ == "__main__":
    unittest.main()
