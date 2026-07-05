from indicators.support_resistance import SupportResistance


class MarketStructure:

    def __init__(self):
        self.sr = SupportResistance()

    def analyze(self, klines, price):
        levels = self.sr.calculate(klines)

        support = levels["support"]
        resistance = levels["resistance"]

        near_support = self.sr.is_near_support(
            price,
            support
        )

        near_resistance = self.sr.is_near_resistance(
            price,
            resistance
        )

        if near_support:
            location = "SUPPORT"

        elif near_resistance:
            location = "RESISTANCE"

        else:
            location = "MIDDLE"

        return {
            "support": support,
            "resistance": resistance,
            "location": location
        }