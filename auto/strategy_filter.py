from strategy.strategy_engine import StrategyEngine
from strategy.trade_direction import TradeDirection


class StrategyFilter:

    def __init__(self):
        self.strategy = StrategyEngine()

    def approve(self, signal_data: dict):
        direction = TradeDirection.from_signal(
            signal_data["signal"]
        )

        if direction is None:
            return {
                "approved": False,
                "reason": "Неизвестный сигнал"
            }

        if direction == TradeDirection.LONG:
            result = self.strategy.validate_long(signal_data)
        else:
            result = self.strategy.validate_short(signal_data)

        if result["allowed"]:
            return {
                "approved": True,
                "direction": direction,
                "reason": "Стратегия подтвердила вход"
            }

        return {
            "approved": False,
            "direction": direction,
            "reason": "; ".join(result["reasons"])
        }