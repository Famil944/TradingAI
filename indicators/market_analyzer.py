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

        df["close"] = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)

        df["EMA20"] = ta.trend.ema_indicator(df["close"], window=20)
        df["EMA50"] = ta.trend.ema_indicator(df["close"], window=50)

        df["RSI"] = ta.momentum.rsi(df["close"], window=14)

        last = df.iloc[-1]

        trend = "📈 Восходящий" if last["EMA20"] > last["EMA50"] else "📉 Нисходящий"

        return {
            "price": last["close"],
            "ema20": round(last["EMA20"], 2),
            "ema50": round(last["EMA50"], 2),
            "rsi": round(last["RSI"], 2),
            "trend": trend
        }