class TradeHistory:

    def __init__(self):
        self.trades = []

    def add(self, trade: dict):
        self.trades.append(trade)

    def all(self):
        return self.trades

    def count(self):
        return len(self.trades)

    def last(self):
        if not self.trades:
            return None
        return self.trades[-1]