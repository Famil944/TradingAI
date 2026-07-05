class MultiTimeframeFilter:

    def __init__(self, multi_tf_analyzer):
        self.multi_tf_analyzer = multi_tf_analyzer
        self.mode = "BALANCED"

    def check(self, symbol: str, direction: str):
        data = self.multi_tf_analyzer.analyze(symbol)

        if direction == "LONG":
            target_signal = "🟢 LONG"
        elif direction == "SHORT":
            target_signal = "🔴 SHORT"
        else:
            return self._failed(symbol, data, direction)

        match_count = 0

        for item in data["results"]:
            if item["signal"] == target_signal:
                match_count += 1

        required = self._required_count()

        approved = (
            match_count >= required
            and abs(data["avg_score"]) >= 60
        )

        return {
            "approved": approved,
            "symbol": symbol,
            "direction": direction,
            "final_signal": data["final_signal"],
            "avg_score": data["avg_score"],
            "match_count": match_count,
            "required": required,
            "details": data["results"]
        }

    def _failed(self, symbol, data, direction):
        return {
            "approved": False,
            "symbol": symbol,
            "direction": direction,
            "final_signal": data["final_signal"],
            "avg_score": data["avg_score"],
            "match_count": 0,
            "required": self._required_count(),
            "details": data["results"]
        }

    def _required_count(self):
        if self.mode == "STRICT":
            return 3

        if self.mode == "BALANCED":
            return 2

        if self.mode == "AGGRESSIVE":
            return 1

        return 2