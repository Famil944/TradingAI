class BacktestTrade:

    def __init__(
        self,
        symbol,
        side,
        entry_price,
        exit_price,
        amount,
        profit,
        profit_percent,
        reason
    ):
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.exit_price = exit_price
        self.amount = amount
        self.profit = profit
        self.profit_percent = profit_percent
        self.reason = reason

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "side": self.side,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "amount": self.amount,
            "profit": self.profit,
            "profit_percent": self.profit_percent,
            "reason": self.reason,
        }