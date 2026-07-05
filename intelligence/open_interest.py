import requests


class OpenInterestService:
    def __init__(self):
        self.base_url = "https://fapi.binance.com"

    def get_open_interest(self, symbol: str = "BTCUSDT") -> dict:
        try:
            url = f"{self.base_url}/fapi/v1/openInterest"
            params = {"symbol": symbol}

            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()

            data = response.json()
            open_interest = float(data["openInterest"])

            return {
                "symbol": symbol,
                "open_interest": round(open_interest, 2),
                "status": self._get_status(open_interest),
                "score_impact": 0,
                "comment": "Open Interest получен. Позже добавим сравнение с прошлым значением.",
            }

        except Exception as e:
            return {
                "symbol": symbol,
                "open_interest": None,
                "status": "❌ Ошибка Open Interest",
                "score_impact": 0,
                "comment": str(e),
            }

    def _get_status(self, open_interest: float) -> str:
        if open_interest > 0:
            return "📊 Open Interest активен"
        return "⚠️ Open Interest пустой"