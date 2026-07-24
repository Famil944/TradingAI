import pandas as pd
import ta


class MarketAnalyzer:
    def analyze(self, klines):
        df = pd.DataFrame(klines)

        df = df.iloc[:, :6]
        df.columns = [
            "time",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]

        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["close"] = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)

        df["EMA20"] = ta.trend.ema_indicator(df["close"], window=20)
        df["EMA50"] = ta.trend.ema_indicator(df["close"], window=50)
        df["EMA200"] = ta.trend.ema_indicator(df["close"], window=200)

        df["RSI"] = ta.momentum.rsi(df["close"], window=14)

        macd = ta.trend.MACD(df["close"])
        df["MACD"] = macd.macd()
        df["MACD_SIGNAL"] = macd.macd_signal()

        df["ATR"] = ta.volatility.average_true_range(
            df["high"],
            df["low"],
            df["close"],
            window=14
        )

        bollinger = ta.volatility.BollingerBands(df["close"])
        df["BB_HIGH"] = bollinger.bollinger_hband()
        df["BB_LOW"] = bollinger.bollinger_lband()

        df["VOLUME_AVG"] = df["volume"].rolling(window=20).mean()

        last = df.iloc[-1]

        atr_percent = (last["ATR"] / last["close"]) * 100
        trend = self._detect_trend(last)
        volume_status = self._detect_volume(last)

        return {
            "price": round(last["close"], 2),
            "ema20": round(last["EMA20"], 2),
            "ema50": round(last["EMA50"], 2),
            "ema200": round(last["EMA200"], 2),
            "rsi": round(last["RSI"], 2),
            "macd": round(last["MACD"], 4),
            "macd_signal": round(last["MACD_SIGNAL"], 4),
            "atr": round(last["ATR"], 2),
            "atr_percent": round(atr_percent, 4),
            "bb_high": round(last["BB_HIGH"], 2),
            "bb_low": round(last["BB_LOW"], 2),
            "volume": round(last["volume"], 2),
            "volume_avg": round(last["VOLUME_AVG"], 2),
            "trend": trend,
            "volume_status": volume_status
        }

    def _detect_trend(self, last):
        if last["EMA20"] > last["EMA50"] > last["EMA200"]:
            return "📈 Сильный восходящий"
        elif last["EMA20"] > last["EMA50"]:
            return "📈 Восходящий"
        elif last["EMA20"] < last["EMA50"] < last["EMA200"]:
            return "📉 Сильный нисходящий"
        elif last["EMA20"] < last["EMA50"]:
            return "📉 Нисходящий"
        else:
            return "➖ Боковик"

    def _detect_volume(self, last):
        if last["volume"] > last["VOLUME_AVG"] * 1.5:
            return "🔥 Высокий объём"
        elif last["volume"] < last["VOLUME_AVG"] * 0.7:
            return "😴 Низкий объём"
        else:
            return "➖ Обычный объём"