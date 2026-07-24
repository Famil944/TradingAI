class StrategyEngine:

    MIN_ATR_PERCENT = 0.25
    MAX_ATR_PERCENT = 3.00

    def _atr_is_valid(self, analysis):
        atr_percent = analysis.get("atr_percent")

        if atr_percent is None:
            return True, None

        if atr_percent < self.MIN_ATR_PERCENT:
            return False, (
                f"ATR слишком низкий ({atr_percent:.2f}%)"
            )

        if atr_percent > self.MAX_ATR_PERCENT:
            return False, (
                f"ATR слишком высокий ({atr_percent:.2f}%)"
            )

        return True, None

    def validate_long(self, analysis: dict):
        reasons = []

        atr_ok, atr_reason = self._atr_is_valid(analysis)
        if not atr_ok:
            reasons.append(atr_reason)

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

        atr_ok, atr_reason = self._atr_is_valid(analysis)
        if not atr_ok:
            reasons.append(atr_reason)

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