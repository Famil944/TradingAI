from services.logger_service import LoggerService
from core.multi_timeframe_analyzer import MultiTimeframeAnalyzer
from auto.multi_tf_filter import MultiTimeframeFilter
from auto.strategy_filter import StrategyFilter
from strategy.strategy_report import StrategyReport


class AutoTrader:

    def __init__(self, scanner, paper, core):
        self.scanner = scanner
        self.paper = paper
        self.logger = LoggerService()
        self.multi_tf = MultiTimeframeAnalyzer(core)
        self.multi_filter = MultiTimeframeFilter(self.multi_tf)
        self.strategy_filter = StrategyFilter()
        self.report = StrategyReport()

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

        strategy_check = self.strategy_filter.approve(best)

        if not strategy_check["approved"]:
            text = (
                f"🚫 Strategy Filter отклонил сделку\n\n"
                f"Монета: {symbol}\n"
                f"Сигнал: {best['signal']}\n"
                f"Причина: {strategy_check['reason']}"
            )
            self.logger.log(strategy_check["reason"])
            return text

        multi_check = self.multi_filter.check(
          symbol=symbol,
          direction=strategy_check["direction"]
          )

        report_text = self.report.build_report(
          symbol=symbol,
          analysis=best,
          strategy_check=strategy_check,
          multi_check=multi_check
          )
        if not multi_check["approved"]:
            self.logger.log("Multi-TF фильтр отклонил сделку.")
            return report_text

        trade_text = self.paper.try_trade_text(best)

        if not trade_text:
            self.logger.log("Сделка не открылась.")
            return "❌ Сделка не открылась."

        self.logger.log("✅ Paper-сделка успешно открыта.")

        return f"🤖 Auto Trader\n\n{trade_text}"