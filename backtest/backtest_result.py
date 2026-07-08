class BacktestResult:

    def __init__(self):
        self.trades = []

    def add_trade(self, trade):
        self.trades.append(trade)

    def summary(self):
        total = len(self.trades)

        if total == 0:
            return {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "winrate": 0,
                "total_profit": 0
            }

        wins = 0
        losses = 0
        total_profit = 0

        for trade in self.trades:
            profit = trade["profit"]
            total_profit += profit

            if profit > 0:
                wins += 1
            else:
                losses += 1

        return {
            "trades": total,
            "wins": wins,
            "losses": losses,
            "winrate": round(wins / total * 100, 2),
            "total_profit": round(total_profit, 2)
        }