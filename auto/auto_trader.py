import threading

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

        candidates = self.selector.select_candidates(
            results, excluded_symbols=open_symbols
        )
        if not candidates:
            text = (
                "🟡 Свободных подходящих LONG/SHORT сигналов нет.\n\n"
                f"Уже открыты: "
                f"{', '.join(sorted(open_symbols)) if open_symbols else 'нет'}\n\n"
                f"{candidates_report}"
            )

            self.logger.log(text)
            return text

        rejected_reports = []
        for candidate in candidates:
            check = self._evaluate_candidate(candidate)
            if check["approved"]:
                trade_signal = dict(candidate)
                trade_signal["strategy"] = "AI_AUTO_V2"
                trade_signal["quality_score"] = check["quality"]["score"]
                try:
                    demo_result = self.demo_controller.open_demo_trade(
                        trade_signal
                    )
                except Exception as error:
                    error_text = (
                        f"Ошибка открытия {candidate['symbol']}: {error}"
                    )
                    self.logger.log(error_text)
                    self._save_signal_check(
                        candidate,
                        check,
                        execution_status="failed",
                        execution_error=str(error),
                    )
                    rejected_reports.append(
                        f"{check['report']}\n\n❌ {error_text}"
                    )
                    continue
                self._save_signal_check(
                    candidate,
                    check,
                    execution_status="opened",
                )
                self.logger.log(demo_result)
                result = f"{check['report']}\n\n{demo_result}"
                self.last_analysis = result
                return result
            self._save_signal_check(
                candidate,
                check,
                execution_status="filtered",
            )
            rejected_reports.append(check["report"])

        result = (
            "🟡 Ни один кандидат не прошёл контролируемые фильтры.\n\n"
            + "\n\n".join(rejected_reports)
        )
        self.last_analysis = result
        return result

    def _evaluate_candidate(self, best):
        symbol = best["symbol"]
        self.logger.log(
            f"Кандидат: {symbol} | Сигнал: {best['signal']} | "
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
        momentum = self.momentum.analyze(
            best, direction=strategy_check.get("direction")
        )
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

        report_text = self.report.build_report(
            symbol=symbol,
            analysis=best,
            strategy_check=strategy_check,
            multi_check=multi_check,
            quality=quality
        )

        minimum_quality = int(self.settings.get("quality_score"))
        approved = (
            final_check["approved"]
            and quality["score"] >= minimum_quality
        )
        if not approved:
            reasons = list(final_check["reasons"])
            if quality["score"] < minimum_quality:
                reasons.append(
                    f"Quality Score ниже настройки: "
                    f"{quality['score']} < {minimum_quality}"
                )
            self.logger.log("; ".join(reasons))
            report_text = (
                f"{report_text}\n\n"
                f"⛔ {'; '.join(reasons)}"
            )
        return {
            "approved": approved,
            "quality": quality,
            "report": report_text,
            "strategy_check": strategy_check,
            "multi_check": multi_check,
            "final_check": {
                **final_check,
                "approved": approved,
                "reasons": reasons if not approved else [],
                "minimum_score": minimum_quality,
            },
        }

    def _save_signal_check(
        self,
        candidate,
        check,
        execution_status,
        execution_error=None,
    ):
        self.signal_log.save_signal_check(
            best=candidate,
            quality=check["quality"],
            strategy_check=check["strategy_check"],
            multi_check=check["multi_check"],
            final_check=check["final_check"],
            execution_status=execution_status,
            execution_error=execution_error,
        )
