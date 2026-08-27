class TradeDirection:

    LONG = "LONG"
    SHORT = "SHORT"

    @staticmethod
    def from_signal(signal: str):
        if signal == "🟢 LONG":
            return TradeDirection.LONG

        if signal == "🔴 SHORT":
            return TradeDirection.SHORT

        return None