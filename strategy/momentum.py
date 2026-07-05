class Momentum:

    def analyze(self, analysis: dict):
        rsi = analysis["rsi"]
        macd = analysis["macd"]
        macd_signal = analysis["macd_signal"]

        score = 0

        if macd > macd_signal:
            score += 40
        else:
            score -= 40

        if 50 <= rsi <= 65:
            score += 30
        elif 35 <= rsi < 50:
            score += 10
        elif rsi > 75:
            score -= 20
        elif rsi < 25:
            score -= 20

        score = max(-100, min(100, score))

        if score >= 50:
            level = "STRONG"

        elif score >= 20:
            level = "MEDIUM"

        elif score >= 0:
            level = "WEAK"

        else:
            level = "NEGATIVE"

        return {
            "score": score,
            "level": level
        }