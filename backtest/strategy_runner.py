from strategy.market_structure import MarketStructure
from strategy.trend_strength import TrendStrength
from strategy.volume_analyzer import VolumeAnalyzer
from strategy.momentum import Momentum
from strategy.false_breakout import FalseBreakout
from strategy.quality_score import QualityScore
from validation.strategy_validator import StrategyValidator
from strategy.final_strategy_validator import FinalStrategyValidator


class StrategyRunner:

    def __init__(self):
        self.market_structure = MarketStructure()
        self.trend_strength = TrendStrength()
        self.volume_analyzer = VolumeAnalyzer()
        self.momentum = Momentum()
        self.false_breakout = FalseBreakout()
        self.quality_score = QualityScore()

        self.validator = StrategyValidator()
        self.final_validator = FinalStrategyValidator()

    def analyze(self, analysis):
        structure = self.market_structure.analyze(analysis)

        trend = self.trend_strength.calculate(
            structure,
            analysis,
        )

        volume = self.volume_analyzer.analyze(analysis)

        momentum = self.momentum.analyze(analysis)

        breakout = self.false_breakout.analyze(analysis)

        quality = self.quality_score.calculate(
            structure=structure,
            trend=trend,
            volume=volume,
            momentum=momentum,
            breakout=breakout,
            multi_tf={"approved": True},
        )

        strategy_check = {
            "approved": True,
            "direction": analysis.get("signal"),
        }

        final = self.final_validator.validate(
            quality=quality,
            strategy_check=strategy_check,
            multi_check={"approved": True},
        )

        return {
            "quality": quality,
            "approved": final["approved"],
            "direction": strategy_check["direction"],
            "reasons": final["reasons"],
        }