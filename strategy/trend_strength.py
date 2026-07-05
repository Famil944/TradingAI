class TrendStrength:

    def calculate(self, structure, analysis):
        score = structure["strength"]

        if analysis["score"] >= 80:
            score += 20
        elif analysis["score"] >= 70:
            score += 10

        score = min(score, 100)

        if score >= 90:
            level = "VERY_STRONG"

        elif score >= 75:
            level = "STRONG"

        elif score >= 55:
            level = "MEDIUM"

        elif score >= 35:
            level = "WEAK"

        else:
            level = "FLAT"

        return {
            "score": score,
            "level": level
        }