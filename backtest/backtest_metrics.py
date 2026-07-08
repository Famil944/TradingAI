class BacktestMetrics:

    def calculate(self, trades, start_balance=1000):
        balance = start_balance
        peak_balance = start_balance
        max_drawdown = 0

        wins = 0
        losses = 0
        gross_profit = 0
        gross_loss = 0

        equity_curve = []

        for trade in trades:
            profit = trade.get("profit", 0)

            balance += profit
            equity_curve.append(balance)

            if balance > peak_balance:
                peak_balance = balance

            drawdown = ((peak_balance - balance) / peak_balance) * 100

            if drawdown > max_drawdown:
                max_drawdown = drawdown

            if profit > 0:
                wins += 1
                gross_profit += profit
            else:
                losses += 1
                gross_loss += abs(profit)

        total = len(trades)

        winrate = 0 if total == 0 else round((wins / total) * 100, 2)
        profit_factor = 0 if gross_loss == 0 else round(gross_profit / gross_loss, 2)

        return {
            "start_balance": start_balance,
            "end_balance": round(balance, 2),
            "total_profit": round(balance - start_balance, 2),
            "trades": total,
            "wins": wins,
            "losses": losses,
            "winrate": winrate,
            "profit_factor": profit_factor,
            "max_drawdown": round(max_drawdown, 2),
            "equity_curve": equity_curve,
        }