from services.logger_service import LoggerService
from core.multi_timeframe_analyzer import MultiTimeframeAnalyzer
from auto.multi_tf_filter import MultiTimeframeFilter
from auto.strategy_filter import StrategyFilter


class AutoTrader:

    def __init__(self, scanner, paper, core):
        self.scanner = scanner
        self.paper = paper
        self.logger = LoggerService()
        self.multi_tf = MultiTimeframeAnalyzer(core)
        self.multi_filter = MultiTimeframeFilter(self.multi_tf)
        self.strategy_filter = StrategyFilter()

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
        symbol = best["symbol"]

        self.logger.log(
            f"Лучшая монета: {symbol} | "
            f"Сигнал: {best['signal']} | "
            f"Score: {best['score']}"
        )

        strategy_check = self.strategy_filter.approve_long(best)

        if not strategy_check["approved"]:
            text = (
                f"🚫 Strategy Filter отклонил сделку\n\n"
                f"Монета: {symbol}\n"
                f"Причина: {strategy_check['reason']}"
            )
            self.logger.log(strategy_check["reason"])
            return text

        multi_check = self.multi_filter.is_strong_long(symbol)

        if not multi_check["approved"]:
            text = (
                f"🟡 Multi-TF не подтвердил вход\n\n"
                f"Монета: {symbol}\n"
                f"Итог: {multi_check['final_signal']}\n"
                f"Средняя оценка: {multi_check['avg_score']}"
            )
            self.logger.log("Multi-TF фильтр отклонил сделку.")
            return text

        trade_text = self.paper.try_trade_text(best)

        if not trade_text:
            self.logger.log("Сделка не открылась.")
            return "❌ Сделка не открылась."

        self.logger.log("✅ Paper-сделка успешно открыта.")

        return f"🤖 Auto Trader\n\n{trade_text}"