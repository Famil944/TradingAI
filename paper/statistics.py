class Statistics:

    def __init__(self):
        self.win = 0
        self.loss = 0

    def add_trade(self, profit):
        if profit > 0:
            self.win += 1
        else:
            self.loss += 1

    def total(self):
        return self.win + self.loss

    def winrate(self):
        total = self.total()

        if total == 0:
            return 0

        return round(self.win / total * 100, 2)