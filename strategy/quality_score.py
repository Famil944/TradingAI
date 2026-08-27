class QualityScore:

    def calculate(
        self,
        structure,
        trend,
        volume,
        momentum,
        breakout,
        multi_tf
    ):
        score = 0

        # Структура рынка (самый важный фактор)
        score += structure["strength"] * 0.30

        # Глобальный тренд
        score += trend["score"] * 0.20

        # Объёмы
        score += volume["score"] * 0.15

        # Импульс
        score += max(momentum["score"], 0) * 0.10

        # Пробой
        score += breakout["score"] * 0.10

        # Подтверждение несколькими ТФ
        if multi_tf["approved"]:
            score += 10
        else:
            score -= 10

        # Штраф за слабую структуру
        if structure["strength"] < 50:
            score -= 10

        # Штраф за отрицательный импульс
        if momentum["score"] < 0:
            score -= 5

        # Штраф за слабый объём
        if volume["score"] < 40:
            score -= 5

        score = max(0, min(round(score, 2), 100))

        if score >= 90:
            rating = "★★★★★"
        elif score >= 80:
            rating = "★★★★☆"
        elif score >= 70:
            rating = "★★★☆☆"
        elif score >= 55:
            rating = "★★☆☆☆"
        else:
            rating = "★☆☆☆☆"

        return {
            "score": score,
            "rating": rating
        }