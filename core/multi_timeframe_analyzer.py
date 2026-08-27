class MultiTimeframeAnalyzer:

    def __init__(self, core):
        self.core = core
        self.timeframes = ["15m", "1h", "4h"]

    def analyze(self, symbol: str):
        results = []

        for timeframe in self.timeframes:
            data = self.core.analyze_symbol(symbol, timeframe)
            results.append(data)

        signals = [item["signal"] for item in results]
        scores = [item["score"] for item in results]

        long_count = signals.count("🟢 LONG")
        short_count = signals.count("🔴 SHORT")

        if long_count == len(self.timeframes):
            final_signal = "🟢 STRONG LONG"
        elif short_count == len(self.timeframes):
            final_signal = "🔴 STRONG SHORT"
        else:
            final_signal = "🟡 WAIT"

        avg_score = round(sum(scores) / len(scores), 2)

        return {
            "symbol": symbol,
            "final_signal": final_signal,
            "avg_score": avg_score,
            "results": results
        }