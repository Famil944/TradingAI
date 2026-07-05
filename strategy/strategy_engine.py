class StrategyEngine:

    def validate_long(self, analysis: dict):
        reasons = []

        if analysis["ema20"] <= analysis["ema50"]:
            reasons.append("EMA не подтверждает LONG")

        if analysis["macd"] <= analysis["macd_signal"]:
            reasons.append("MACD медвежий")

        if not (35 <= analysis["rsi"] <= 60):
            reasons.append("RSI вне рабочей зоны")

        if analysis["score"] < 60:
            reasons.append("Недостаточный Score")

        allowed = len(reasons) == 0

        return {
            "allowed": allowed,
            "reasons": reasons
        }