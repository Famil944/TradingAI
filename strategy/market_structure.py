class MarketStructure:

    def analyze(self, analysis: dict):
        ema20 = analysis["ema20"]
        ema50 = analysis["ema50"]
        price = analysis["price"]

        if price > ema20 > ema50:
            return {
                "trend": "UPTREND",
                "strength": 100
            }

        if price < ema20 < ema50:
            return {
                "trend": "DOWNTREND",
                "strength": 100
            }

        if ema20 > ema50:
            return {
                "trend": "WEAK_UPTREND",
                "strength": 60
            }

        if ema20 < ema50:
            return {
                "trend": "WEAK_DOWNTREND",
                "strength": 60
            }

        return {
            "trend": "RANGE",
            "strength": 20
        }