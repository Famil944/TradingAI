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

        if analysis.get("location") == "RESISTANCE":
            reasons.append("Цена слишком близко к сопротивлению")

        return {
            "allowed": len(reasons) == 0,
            "reasons": reasons
        }

    def validate_short(self, analysis: dict):
        reasons = []

        if analysis["ema20"] >= analysis["ema50"]:
            reasons.append("EMA не подтверждает SHORT")

        if analysis["macd"] >= analysis["macd_signal"]:
            reasons.append("MACD бычий")

        if not (40 <= analysis["rsi"] <= 65):
            reasons.append("RSI вне рабочей зоны")

        if analysis["score"] < 60:
            reasons.append("Недостаточный Score")

        if analysis.get("location") == "SUPPORT":
            reasons.append("Цена слишком близко к поддержке")

        return {
            "allowed": len(reasons) == 0,
            "reasons": reasons
        }