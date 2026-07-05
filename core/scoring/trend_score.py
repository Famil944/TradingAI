class TrendScore:

    @staticmethod
    def calculate(analysis: dict, reasons: list) -> int:
        ema20 = analysis["ema20"]
        ema50 = analysis["ema50"]
        ema200 = analysis["ema200"]

        if ema20 > ema50 > ema200:
            reasons.append("EMA показывает сильный восходящий тренд")
            return 25

        if ema20 > ema50:
            reasons.append("EMA показывает восходящий тренд")
            return 15

        if ema20 < ema50 < ema200:
            reasons.append("EMA показывает сильный нисходящий тренд")
            return -25

        if ema20 < ema50:
            reasons.append("EMA показывает нисходящий тренд")
            return -15

        return 0