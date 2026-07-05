class AutoTrader:

    def __init__(self, scanner, paper):
        self.scanner = scanner
        self.paper = paper

    def run_once(self):
        status = self.paper.engine.status()

        if status["has_position"]:
            return "📄 Уже есть открытая Paper-сделка. Новую не открываю."

        results = self.scanner.scan_market("1h", 5)

        if not results:
            return "❌ Авто-режим не нашёл сигналов."

        best = results[0]

        if best["signal"] != "🟢 LONG":
            return (
                f"🟡 Лучший сигнал сейчас не LONG\n\n"
                f"Монета: {best['symbol']}\n"
                f"Сигнал: {best['signal']}\n"
                f"Оценка: {best['score']} / 100"
            )

        trade_text = self.paper.try_trade_text(best)

        if not trade_text:
            return "❌ Сигнал LONG найден, но Paper-сделка не открылась."

        return (
            f"🤖 Auto Trader нашёл сделку\n\n"
            f"{trade_text}"
        )