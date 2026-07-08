from services.logger_service import LoggerService
from core.multi_timeframe_analyzer import MultiTimeframeAnalyzer
from auto.multi_tf_filter import MultiTimeframeFilter
from auto.strategy_filter import StrategyFilter
from auto.candidate_selector import CandidateSelector
from auto.candidate_report import CandidateReport
from strategy.strategy_report import StrategyReport

from strategy.market_structure import MarketStructure
from strategy.trend_strength import TrendStrength
from strategy.volume_analyzer import VolumeAnalyzer
from strategy.momentum import Momentum
from strategy.false_breakout import FalseBreakout
from strategy.quality_score import QualityScore
from validation.strategy_validator import StrategyValidator


class AutoTrader:

    def __init__(self, scanner, paper, core):
        self.scanner = scanner
        self.paper = paper
        self.logger = LoggerService()

        self.multi_tf = MultiTimeframeAnalyzer(core)
        self.multi_filter = MultiTimeframeFilter(self.multi_tf)

        self.strategy_filter = StrategyFilter()
        self.selector = CandidateSelector()
        self.candidate_report = CandidateReport()
        self.report = StrategyReport()

        self.market_structure = MarketStructure()
        self.trend_strength = TrendStrength()
        self.volume_analyzer = VolumeAnalyzer()
        self.momentum = Momentum()
        self.false_breakout = FalseBreakout()
        self.quality_score = QualityScore()
        self.validator = StrategyValidator()

    def run_once(self):
        self.logger.log("🤖 Начало автоматического сканирования")

        status = self.paper.engine.status()

        if status["has_position"]:
            text = "📄 Уже есть открытая Paper-сделка."
            self.logger.log(text)
            return text

        results = self.scanner.scan_market("1h", 5)
        candidates_report = self.candidate_report.build(results)
        self.logger.log(candidates_report)

        if not results:
            text = "❌ Сигналы не найдены."
            self.logger.log(text)
            return text

        best = self.selector.select_best(results)

        if best is None:
            text = (
                "🟡 Подходящих LONG/SHORT сигналов нет.\n\n"
                f"{candidates_report}"
            )
            self.logger.log(text)
            return text

        symbol = best["symbol"]

        self.logger.log(
            f"Лучшая монета: {symbol} | "
            f"Сигнал: {best['signal']} | "
            f"Score: {best['score']}"
        )

        strategy_check = self.strategy_filter.approve(best)

        multi_check = self.multi_filter.check(
            symbol=symbol,
            direction=strategy_check.get("direction")
        )

        structure = self.market_structure.analyze(best)
        trend = self.trend_strength.calculate(structure, best)
        volume = self.volume_analyzer.analyze(best)
        momentum = self.momentum.analyze(best)
        breakout = self.false_breakout.analyze(best)

        quality = self.quality_score.calculate(
            structure=structure,
            trend=trend,
            volume=volume,
            momentum=momentum,
            breakout=breakout,
            multi_tf=multi_check
        )
        
        self.validator.validate(
            strategy_check,
            multi_check,
            quality
        )     

        report_text = self.report.build_report(
            symbol=symbol,
            analysis=best,
            strategy_check=strategy_check,
            multi_check=multi_check,
            quality=quality
        )

        if not strategy_check["approved"]:
            self.logger.log(strategy_check["reason"])
            return report_text

        if not multi_check["approved"]:
            self.logger.log("Multi-TF фильтр отклонил сделку.")
            return report_text

        trade_text = self.paper.try_trade_text(best)

        if not trade_text:
            self.logger.log("Сделка не открылась.")
            return "❌ Сделка не открылась."

        self.logger.log("✅ Paper-сделка успешно открыта.")

        return f"🤖 Auto Trader\n\n{trade_text}"