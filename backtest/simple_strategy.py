class SimpleStrategy:

    def get_signal(self, candle):
        close = candle["close"]
        open_price = candle["open"]

        if close > open_price:
            return "LONG"

        if close < open_price:
            return "SHORT"

        return None