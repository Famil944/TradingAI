class MultiTimeframeFilter:

    def __init__(self, multi_tf_analyzer):
        self.multi_tf_analyzer = multi_tf_analyzer

    def is_strong_long(self, symbol: str):
        data = self.multi_tf_analyzer.analyze(symbol)

        return {
            "approved": data["final_signal"] == "🟢 STRONG LONG",
            "symbol": symbol,
            "final_signal": data["final_signal"],
            "avg_score": data["avg_score"],
            "details": data["results"]
        }