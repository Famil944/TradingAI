class PaperPosition:

    def __init__(self, symbol, side, entry_price, amount):
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.amount = amount
        self.is_open = True

    def close(self, exit_price):
        self.exit_price = exit_price
        self.is_open = False

        if self.side == "LONG":
            return (exit_price - self.entry_price) * self.amount

        if self.side == "SHORT":
            return (self.entry_price - exit_price) * self.amount

        return 0