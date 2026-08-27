class FalseBreakout:

    def analyze(self, analysis: dict):
        price = analysis["price"]
        resistance = analysis.get("resistance")
        support = analysis.get("support")
        signal = analysis["signal"]

        if signal == "🟢 LONG":
            if resistance and price >= resistance * 0.995:
                return {
                    "risk": "HIGH",
                    "score": 20
                }

            return {
                "risk": "LOW",
                "score": 100
            }

        if signal == "🔴 SHORT":
            if support and price <= support * 1.005:
                return {
                    "risk": "HIGH",
                    "score": 20
                }

            return {
                "risk": "LOW",
                "score": 100
            }

        return {
            "risk": "UNKNOWN",
            "score": 50
        }