class AnalysisAdapter:

    def build(self, symbol, candles):
        current = candles[-1]
        closes = [c["close"] for c in candles]
        volumes = [c["volume"] for c in candles]

        ema20 = self._ema(closes, 20)
        ema50 = self._ema(closes, 50)
        ema200 = self._ema(closes, 200)
        rsi = self._rsi(closes)

        macd = ema20 - ema50
        macd_signal = macd * 0.8

        price = current["close"]
        volume = current["volume"]
        volume_avg = sum(volumes[-20:]) / min(len(volumes), 20)

        score = 0
        signal = None
        reasons = []

        if price > ema20 > ema50:
            signal = "🟢 LONG"
            score += 40
            reasons.append("Цена выше EMA20 и EMA50")

        elif price < ema20 < ema50:
            signal = "🔴 SHORT"
            score += 40
            reasons.append("Цена ниже EMA20 и EMA50")

        if macd > macd_signal:
            score += 20
            reasons.append("MACD подтверждает рост")
        elif macd < macd_signal:
            score += 20
            reasons.append("MACD подтверждает падение")

        if 35 <= rsi <= 65:
            score += 20
            reasons.append("RSI в рабочей зоне")

        if volume >= volume_avg:
            score += 20
            reasons.append("Объём нормальный")

        return {
            "symbol": symbol,
            "interval": "1h",
            "price": price,
            "signal": signal,
            "score": score,
            "trend": "UP" if price > ema50 else "DOWN",
            "volume": volume,
            "volume_avg": volume_avg,
            "volume_status": "NORMAL",
            "ema20": round(ema20, 4),
            "ema50": round(ema50, 4),
            "ema200": round(ema200, 4),
            "rsi": round(rsi, 2),
            "macd": round(macd, 4),
            "macd_signal": round(macd_signal, 4),
            "atr": 0,
            "bb_high": current["high"],
            "bb_low": current["low"],
            "support": min(c["low"] for c in candles[-20:]),
            "resistance": max(c["high"] for c in candles[-20:]),
            "risk": "Средний",
            "reasons": reasons,
        }

    def _ema(self, values, period):
        if len(values) < period:
            return values[-1]

        k = 2 / (period + 1)
        ema = values[0]

        for price in values:
            ema = price * k + ema * (1 - k)

        return ema

    def _rsi(self, closes, period=14):
        if len(closes) <= period:
            return 50

        gains = []
        losses = []

        for i in range(-period, 0):
            diff = closes[i] - closes[i - 1]

            if diff >= 0:
                gains.append(diff)
            else:
                losses.append(abs(diff))

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        if avg_loss == 0:
            return 100

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
