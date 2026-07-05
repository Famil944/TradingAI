class MacdScore:

    @staticmethod
    def calculate(analysis: dict, reasons: list) -> int:
        macd = analysis["macd"]
        signal = analysis["macd_signal"]

        if macd > signal:
            reasons.append("MACD выше сигнальной линии")
            return 15

        reasons.append("MACD ниже сигнальной линии")
        return -15