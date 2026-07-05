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

        score += structure["strength"] * 0.25
        score += trend["score"] * 0.20
        score += volume["score"] * 0.20
        score += max(momentum["score"], 0) * 0.15
        score += breakout["score"] * 0.10

        if multi_tf["approved"]:
            score += 10

        score = round(min(score, 100), 2)

        if score >= 90:
            rating = "★★★★★"

        elif score >= 75:
            rating = "★★★★☆"

        elif score >= 60:
            rating = "★★★☆☆"

        elif score >= 40:
            rating = "★★☆☆☆"

        else:
            rating = "★☆☆☆☆"

        return {
            "score": score,
            "rating": rating
        }