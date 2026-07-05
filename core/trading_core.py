from exchange.binance_client import BinanceMarketClient
from indicators.market_analyzer import MarketAnalyzer


class TradingCore:
    def __init__(self):
        self.market = BinanceMarketClient()
        self.analyzer = MarketAnalyzer()

    def analyze_symbol(self, symbol: str = "BTCUSDT", interval: str = "1h") -> dict:
        klines = self.market.get_klines(symbol, interval, 250)
        analysis = self.analyzer.analyze(klines)

        decision = self._make_decision(analysis)

        return {
            "symbol": symbol,
            "interval": interval,
            **analysis,
            **decision
        }

    def _make_decision(self, analysis: dict) -> dict:
        score = 0
        reasons = []

        price = analysis["price"]
        ema20 = analysis["ema20"]
        ema50 = analysis["ema50"]
        ema200 = analysis["ema200"]
        rsi = analysis["rsi"]
        macd = analysis["macd"]
        macd_signal = analysis["macd_signal"]
        bb_high = analysis["bb_high"]
        bb_low = analysis["bb_low"]
        volume = analysis["volume"]
        volume_avg = analysis["volume_avg"]

        if ema20 > ema50 > ema200:
            score += 25
            reasons.append("EMA показывает сильный восходящий тренд")
        elif ema20 > ema50:
            score += 15
            reasons.append("EMA показывает восходящий тренд")
        elif ema20 < ema50 < ema200:
            score -= 25
            reasons.append("EMA показывает сильный нисходящий тренд")
        elif ema20 < ema50:
            score -= 15
            reasons.append("EMA показывает нисходящий тренд")

        if 45 <= rsi <= 60:
            score += 10
            reasons.append("RSI в здоровой зоне для продолжения роста")
        elif 30 <= rsi < 45:
            score += 5
            reasons.append("RSI ниже середины, возможен отскок")
        elif rsi < 30:
            score += 10
            reasons.append("RSI показывает перепроданность")
        elif 60 < rsi <= 70:
            score -= 5
            reasons.append("RSI близко к перекупленности")
        elif rsi > 70:
            score -= 15
            reasons.append("RSI показывает перекупленность")

        if macd > macd_signal:
            score += 15
            reasons.append("MACD выше сигнальной линии")
        else:
            score -= 15
            reasons.append("MACD ниже сигнальной линии")

        if volume > volume_avg * 1.3:
            score += 10
            reasons.append("Объём выше среднего")
        elif volume < volume_avg * 0.7:
            score -= 5
            reasons.append("Объём ниже среднего")

        if price <= bb_low:
            score += 10
            reasons.append("Цена возле нижней Bollinger Band")
        elif price >= bb_high:
            score -= 10
            reasons.append("Цена возле верхней Bollinger Band")

        if score >= 45:
            signal = "🟢 LONG"
            risk = "Средний"
        elif score <= -45:
            signal = "🔴 SHORT"
            risk = "Средний"
        else:
            signal = "🟡 WAIT"
            risk = "Низкий"

        score = max(min(score, 100), -100)

        return {
            "signal": signal,
            "score": score,
            "risk": risk,
            "reasons": reasons
        }