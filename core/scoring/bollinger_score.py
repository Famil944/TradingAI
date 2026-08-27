class BollingerScore:

    @staticmethod
    def calculate(analysis: dict, reasons: list) -> int:
        price = analysis["price"]
        bb_high = analysis["bb_high"]
        bb_low = analysis["bb_low"]

        if price <= bb_low:
            reasons.append("Цена возле нижней Bollinger Band")
            return 10

        if price >= bb_high:
            reasons.append("Цена возле верхней Bollinger Band")
            return -10

        return 0