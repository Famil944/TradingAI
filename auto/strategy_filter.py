from strategy.strategy_engine import StrategyEngine


class StrategyFilter:

    def __init__(self):
        self.strategy = StrategyEngine()

    def approve_long(self, signal_data: dict):
        if signal_data["signal"] != "🟢 LONG":
            return {
                "approved": False,
                "reason": "Сигнал не LONG"
            }

        result = self.strategy.validate_long(signal_data)

        if result["allowed"]:
            return {
                "approved": True,
                "reason": "Стратегия подтвердила LONG"
            }

        return {
            "approved": False,
            "reason": "; ".join(result["reasons"])
        }