from services.logger_service import LoggerService


class AutoTrader:

    def __init__(self, scanner, paper):
        self.scanner = scanner
        self.paper = paper
        self.logger = LoggerService()

    def run_once(self):
        self.logger.log("🤖 Начало автоматического сканирования")

        status = self.paper.engine.status()

        if status["has_position"]:
            text = "📄 Уже есть открытая Paper-сделка."
            self.logger.log(text)
            return text

        results = self.scanner.scan_market("1h", 5)

        if not results:
            text = "❌ Сигналы не найдены."
            self.logger.log(text)
            return text

        best = results[0]

        self.logger.log(
            f"Лучшая монета: {best['symbol']} | "
            f"Сигнал: {best['signal']} | "
            f"Score: {best['score']}"
        )

        if best["signal"] != "🟢 LONG":
            text = (
                f"🟡 Лучший сигнал сейчас не LONG\n\n"
                f"Монета: {best['symbol']}\n"
                f"Сигнал: {best['signal']}\n"
                f"Оценка: {best['score']}"
            )
            self.logger.log("LONG не найден.")
            return text

        trade_text = self.paper.try_trade_text(best)

        if not trade_text:
            self.logger.log("LONG найден, но сделка не открылась.")
            return "❌ Сделка не открылась."

        self.logger.log("✅ Paper-сделка успешно открыта.")

        return f"🤖 Auto Trader\n\n{trade_text}"