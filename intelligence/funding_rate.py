import requests


class FundingRateService:
    def __init__(self):
        self.base_url = "https://fapi.binance.com"

    def get_funding_rate(self, symbol: str = "BTCUSDT") -> dict:
        try:
            url = f"{self.base_url}/fapi/v1/premiumIndex"
            params = {"symbol": symbol}

            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()

            data = response.json()

            funding_rate = float(data["lastFundingRate"]) * 100

            return {
                "symbol": symbol,
                "funding_rate": round(funding_rate, 4),
                "status": self._get_status(funding_rate),
                "score_impact": self._get_score_impact(funding_rate),
                "comment": self._get_comment(funding_rate),
            }

        except Exception as e:
            return {
                "symbol": symbol,
                "funding_rate": None,
                "status": "❌ Ошибка Funding Rate",
                "score_impact": 0,
                "comment": str(e),
            }

    def _get_status(self, funding_rate: float) -> str:
        if funding_rate > 0.05:
            return "⚠️ Слишком много лонгов"
        elif funding_rate < -0.05:
            return "⚠️ Слишком много шортов"
        else:
            return "✅ Funding нормальный"

    def _get_score_impact(self, funding_rate: float) -> int:
        if funding_rate > 0.05:
            return -10
        elif funding_rate < -0.05:
            return 10
        else:
            return 0

    def _get_comment(self, funding_rate: float) -> str:
        if funding_rate > 0.05:
            return "Толпа сильно лонгует. Повышен риск резкого падения."
        elif funding_rate < -0.05:
            return "Толпа сильно шортит. Возможен резкий отскок вверх."
        else:
            return "Funding спокойный. Сильного перекоса нет."