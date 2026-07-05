class VolumeScore:

    @staticmethod
    def calculate(analysis: dict, reasons: list) -> int:
        volume = analysis["volume"]
        volume_avg = analysis["volume_avg"]

        if volume > volume_avg * 1.3:
            reasons.append("Объём выше среднего")
            return 10

        if volume < volume_avg * 0.7:
            reasons.append("Объём ниже среднего")
            return -5

        return 0