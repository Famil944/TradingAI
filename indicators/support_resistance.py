class SupportResistance:

    def calculate(self, klines):
        highs = []
        lows = []

        for candle in klines[-50:]:
            highs.append(float(candle[2]))
            lows.append(float(candle[3]))

        resistance = max(highs)
        support = min(lows)

        return {
            "support": round(support, 4),
            "resistance": round(resistance, 4)
        }

    def is_near_resistance(self, price, resistance, percent=0.5):
        if price is None or price <= 0:
            return False

        distance = ((resistance - price) / price) * 100
        return 0 <= distance <= percent

    def is_near_support(self, price, support, percent=0.5):
        if price is None or price <= 0:
            return False

        distance = ((price - support) / price) * 100
        return 0 <= distance <= percent