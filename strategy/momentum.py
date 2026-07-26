class Momentum:

    def analyze(self, analysis: dict, direction=None):
        rsi = analysis["rsi"]
        macd = analysis["macd"]
        macd_signal = analysis["macd_signal"]

        direction = direction or (
            "SHORT" if analysis.get("signal") == "🔴 SHORT" else "LONG"
        )
        score = 0

        macd_confirms = (
            macd > macd_signal
            if direction == "LONG"
            else macd < macd_signal
        )
        if macd_confirms:
            score += 40
        else:
            score -= 40

        if direction == "LONG":
            if 50 <= rsi <= 65:
                score += 30
            elif 35 <= rsi < 50:
                score += 10
            elif rsi > 75 or rsi < 25:
                score -= 20
        else:
            if 35 <= rsi <= 50:
                score += 30
            elif 50 < rsi <= 65:
                score += 10
            elif rsi > 75 or rsi < 25:
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
