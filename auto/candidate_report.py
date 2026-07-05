class CandidateReport:

    def build(self, results: list):
        if not results:
            return "Кандидатов нет."

        text = "📋 Топ кандидатов\n\n"

        for index, item in enumerate(results[:5], start=1):
            text += (
                f"{index}. {item['symbol']}\n"
                f"Сигнал: {item['signal']}\n"
                f"Score: {item['score']} / 100\n"
                f"Цена: {item['price']}\n\n"
            )

        return text