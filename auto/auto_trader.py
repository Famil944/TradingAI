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
from strategy.final_strategy_validator import FinalStrategyValidator
from services.signal_log_service import SignalLogService
from services.demo_trading_controller import DemoTradingController
from services.app_settings import AppSettings


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
        self.final_validator = FinalStrategyValidator()
        self.signal_log = SignalLogService()
        self.demo_controller = DemoTradingController()
        self.settings = AppSettings()
        self.last_analysis = None
        self._run_lock = threading.Lock()

    def run_once(self):
        if not self._run_lock.acquire(blocking=False):
            return "⏳ Автосканирование уже выполняется."
        try:
            return self._run_once()
        finally:
            self._run_lock.release()

    def _run_once(self):
        self.logger.log("🤖 Начало автоматического сканирования")

        open_positions = self.demo_controller.client.open_positions()

        open_symbols = {
            position["symbol"]
            for position in open_positions
        }

        self.logger.log(
            "Открытые Demo-позиции: "
            + (
                ", ".join(sorted(open_symbols))
                if open_symbols
                else "нет"
            )
        )

        results = self.scanner.scan_market(
            self.settings.get("timeframe"),
            5,
        )
        candidates_report = self.candidate_report.build(results)
        self.last_analysis = candidates_report
        self.logger.log(candidates_report)

        if not results:
            text = "❌ Сигналы не найдены."
            self.logger.log(text)
            return text

        best = self.selector.select_best(
            results,
            excluded_symbols=open_symbols,
        )

        if best is None:
            text = (
                "🟡 Свободных подходящих LONG/SHORT сигналов нет.\n\n"
                f"Уже открыты: "
                f"{', '.join(sorted(open_symbols)) if open_symbols else 'нет'}\n\n"
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

        final_check = self.final_validator.validate(
            quality=quality,
            strategy_check=strategy_check,
            multi_check=multi_check
        )

        self.validator.validate(
            strategy_check,
            multi_check,
            quality
        )

        self.signal_log.save_signal_check(
            best=best,
            quality=quality,
            strategy_check=strategy_check,
            multi_check=multi_check,
            final_check=final_check,
        )

        report_text = self.report.build_report(
            symbol=symbol,
            analysis=best,
            strategy_check=strategy_check,
            multi_check=multi_check,
            quality=quality
        )

        if not final_check["approved"]:
            self.logger.log("; ".join(final_check["reasons"]))
            return report_text

        minimum_quality = int(self.settings.get("quality_score"))
        if quality["score"] < minimum_quality:
            text = (
                f"{report_text}\n\n"
                f"⛔ Quality Score ниже настройки: "
                f"{quality['score']} < {minimum_quality}"
            )
            self.last_analysis = text
            return text

        trade_signal = dict(best)

        trade_signal["strategy"] = "AI_AUTO_V2"
        trade_signal["quality_score"] = quality["score"]

        demo_result = self.demo_controller.open_demo_trade(
            trade_signal
        )

        self.logger.log(demo_result)

        result = (
            f"{report_text}\n\n"
            f"{demo_result}"
        )
        self.last_analysis = result
        return result
import threading
