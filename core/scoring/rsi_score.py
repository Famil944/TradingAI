class RsiScore:

    @staticmethod
    def calculate(analysis: dict, reasons: list) -> int:
        rsi = analysis["rsi"]

        if 45 <= rsi <= 60:
            reasons.append("RSI в здоровой зоне")
            return 10

        if 30 <= rsi < 45:
            reasons.append("RSI ниже середины, возможен отскок")
            return 5

        if rsi < 30:
            reasons.append("RSI показывает перепроданность")
            return 10

        if 60 < rsi <= 70:
            reasons.append("RSI близко к перекупленности")
            return -5

        if rsi > 70:
            reasons.append("RSI показывает перекупленность")
            return -15

        return 0