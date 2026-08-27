from strategy.strategy_engine import StrategyEngine


class ShortStrategyFilter:

    def __init__(self):
        self.strategy = StrategyEngine()

    def approve_short(self, signal_data: dict):
        if signal_data["signal"] != "🔴 SHORT":
            return {
                "approved": False,
                "reason": "Сигнал не SHORT"
            }

        result = self.strategy.validate_short(signal_data)

        if result["allowed"]:
            return {
                "approved": True,
                "reason": "Стратегия подтвердила SHORT"
            }

        return {
            "approved": False,
            "reason": "; ".join(result["reasons"])
        }