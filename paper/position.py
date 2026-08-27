class PaperPosition:

    def __init__(self, symbol, side, entry_price, amount, take_profit=None, stop_loss=None):
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.amount = amount
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.partial_closed = False
        self.is_open = True

    def update_stop_loss(self, new_stop_loss):
        self.stop_loss = new_stop_loss

    def close_partial(self, close_amount, exit_price):
        if close_amount <= 0 or close_amount > self.amount:
            return 0

        self.amount -= close_amount
        self.partial_closed = True

        if self.side == "LONG":
            return (exit_price - self.entry_price) * close_amount

        if self.side == "SHORT":
            return (self.entry_price - exit_price) * close_amount

        return 0

    def close(self, exit_price):
        self.exit_price = exit_price
        self.is_open = False

        if self.side == "LONG":
            return (exit_price - self.entry_price) * self.amount

        if self.side == "SHORT":
            return (self.entry_price - exit_price) * self.amount

        return 0