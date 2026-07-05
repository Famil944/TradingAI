class ScoringEngine:
    def make_decision(
        self,
        analysis: dict,
        fear_greed_data: dict,
        funding_data: dict,
        open_interest_data: dict
    ) -> dict:
        score = 0
        reasons = []

        score += self._score_trend(analysis, reasons)
        score += self._score_rsi(analysis, reasons)
        score += self._score_macd(analysis, reasons)
        score += self._score_volume(analysis, reasons)
        score += self._score_bollinger(analysis, reasons)
        score += self._score_fear_greed(fear_greed_data, reasons)
        score += self._score_funding(funding_data, reasons)
        self._add_open_interest_reason(open_interest_data, reasons)

        score = max(min(score, 100), -100)

        if score >= 45:
            signal = "🟢 LONG"
            risk = "Средний"
        elif score <= -45:
            signal = "🔴 SHORT"
            risk = "Средний"
        else:
            signal = "🟡 WAIT"
            risk = "Низкий"

        return {
            "signal": signal,
            "score": score,
            "risk": risk,
            "reasons": reasons
        }

    def _score_trend(self, analysis: dict, reasons: list) -> int:
        ema20 = analysis["ema20"]
        ema50 = analysis["ema50"]
        ema200 = analysis["ema200"]

        if ema20 > ema50 > ema200:
            reasons.append("EMA показывает сильный восходящий тренд")
            return 25

        if ema20 > ema50:
            reasons.append("EMA показывает восходящий тренд")
            return 15

        if ema20 < ema50 < ema200:
            reasons.append("EMA показывает сильный нисходящий тренд")
            return -25

        if ema20 < ema50:
            reasons.append("EMA показывает нисходящий тренд")
            return -15

        return 0

    def _score_rsi(self, analysis: dict, reasons: list) -> int:
        rsi = analysis["rsi"]

        if 45 <= rsi <= 60:
            reasons.append("RSI в здоровой зоне")
            return 10

        if 30 <= rsi < 45:
            reasons.append("RSI ниже середины, возможен отскок")
            return 5

        if rsi < 30:
            reasons.append("RSI показывает перепроданность")
            return 10

        if 60 < rsi <= 70:
            reasons.append("RSI близко к перекупленности")
            return -5

        if rsi > 70:
            reasons.append("RSI показывает перекупленность")
            return -15

        return 0

    def _score_macd(self, analysis: dict, reasons: list) -> int:
        if analysis["macd"] > analysis["macd_signal"]:
            reasons.append("MACD выше сигнальной линии")
            return 15

        reasons.append("MACD ниже сигнальной линии")
        return -15

    def _score_volume(self, analysis: dict, reasons: list) -> int:
        volume = analysis["volume"]
        volume_avg = analysis["volume_avg"]

        if volume > volume_avg * 1.3:
            reasons.append("Объём выше среднего")
            return 10

        if volume < volume_avg * 0.7:
            reasons.append("Объём ниже среднего")
            return -5

        return 0

    def _score_bollinger(self, analysis: dict, reasons: list) -> int:
        price = analysis["price"]

        if price <= analysis["bb_low"]:
            reasons.append("Цена возле нижней Bollinger Band")
            return 10

        if price >= analysis["bb_high"]:
            reasons.append("Цена возле верхней Bollinger Band")
            return -10

        return 0

    def _score_fear_greed(self, data: dict, reasons: list) -> int:
        impact = data.get("score_impact", 0)

        if impact > 0:
            reasons.append("Fear & Greed добавляет плюс к сигналу")
        elif impact < 0:
            reasons.append("Fear & Greed предупреждает о перегреве рынка")

        return impact

    def _score_funding(self, data: dict, reasons: list) -> int:
        impact = data.get("score_impact", 0)
        if impact > 0:
            reasons.append("Funding показывает возможный отскок вверх")
        elif impact < 0:
            reasons.append("Funding предупреждает о перегреве лонгов")

        return impact

    def _add_open_interest_reason(self, data: dict, reasons: list):
        if data.get("open_interest"):
            reasons.append("Open Interest активен, рынок имеет фьючерсную ликвидность")