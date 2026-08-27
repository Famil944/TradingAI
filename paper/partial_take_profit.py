class PartialTakeProfit:

    def __init__(self, trigger_percent=0.8, close_percent=50):
        self.trigger_percent = trigger_percent
        self.close_percent = close_percent

    def should_take_profit(self, position, current_price):
        if hasattr(position, "partial_closed") and position.partial_closed:
            return False

        if position.side == "LONG":
            profit_percent = (
                (current_price - position.entry_price)
                / position.entry_price
            ) * 100

        elif position.side == "SHORT":
            profit_percent = (
                (position.entry_price - current_price)
                / position.entry_price
            ) * 100

        else:
            return False

        return profit_percent >= self.trigger_percent

    def calculate_close_amount(self, position):
        return round(position.amount * (self.close_percent / 100), 6)