class RiskRules:

    def __init__(self):
        self.take_profit_percent = 1.0
        self.stop_loss_percent = 0.5

    def get_levels(self, entry_price: float, side: str):
        if side == "LONG":
            take_profit = entry_price * (1 + self.take_profit_percent / 100)
            stop_loss = entry_price * (1 - self.stop_loss_percent / 100)
        else:
            take_profit = entry_price * (1 - self.take_profit_percent / 100)
            stop_loss = entry_price * (1 + self.stop_loss_percent / 100)

        return {
            "take_profit": round(take_profit, 4),
            "stop_loss": round(stop_loss, 4)
        }

    def should_close(self, position, current_price: float):
        levels = self.get_levels(position.entry_price, position.side)

        if position.side == "LONG":
            if current_price >= levels["take_profit"]:
                return "TAKE_PROFIT"

            if current_price <= levels["stop_loss"]:
                return "STOP_LOSS"

        return None