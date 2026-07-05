import requests


class FearGreedService:
    def __init__(self):
        self.url = "https://api.alternative.me/fng/"

    def get_index(self):
        try:
            response = requests.get(self.url, timeout=15)
            response.raise_for_status()

            data = response.json()
            item = data["data"][0]

            value = int(item["value"])
            classification = item["value_classification"]

            return {
                "value": value,
                "classification": classification,
                "status": self._get_status(value),
                "score_impact": self._get_score_impact(value),
                "comment": self._get_comment(value),
            }

        except Exception as e:
            return {
                "value": None,
                "classification": "Ошибка",
                "status": "❌ Не удалось получить Fear & Greed Index",
                "score_impact": 0,
                "comment": str(e),
            }

    def _get_status(self, value):
        if value <= 25:
            return "😨 Extreme Fear"
        elif value <= 45:
            return "😟 Fear"
        elif value <= 55:
            return "😐 Neutral"
        elif value <= 75:
            return "🤑 Greed"
        else:
            return "🚨 Extreme Greed"

    def _get_score_impact(self, value):
        if value <= 25:
            return 15
        elif value <= 45:
            return 5
        elif value <= 55:
            return 0
        elif value <= 75:
            return -5
        else:
            return -15

    def _get_comment(self, value):
        if value <= 25:
            return "Рынок в сильном страхе. Возможны хорошие зоны для набора, но риск высокий."
        elif value <= 45:
            return "На рынке страх. Лучше искать осторожные входы."
        elif value <= 55:
            return "Рынок нейтральный. Сильного преимущества нет."
        elif value <= 75:
            return "На рынке жадность. Нужно быть осторожнее с лонгами."
        else:
            return "Сильная жадность. Повышен риск коррекции."