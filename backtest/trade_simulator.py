from backtest.backtest_trade import BacktestTrade


class TradeSimulator:

    def __init__(self):
        self.take_profit_percent = 1.0
        self.stop_loss_percent = 0.5
        self.trade_size = 100

    def simulate(self, symbol, side, entry_candle, future_candles):
        entry_price = entry_candle["close"]
        amount = self.trade_size / entry_price

        levels = self._levels(entry_price, side)

        for candle in future_candles:
            high = candle["high"]
            low = candle["low"]

            if side == "LONG":
                if low <= levels["stop_loss"]:
                    return self._close(
                        symbol, side, entry_price,
                        levels["stop_loss"], amount, "STOP_LOSS"
                    )

                if high >= levels["take_profit"]:
                    return self._close(
                        symbol, side, entry_price,
                        levels["take_profit"], amount, "TAKE_PROFIT"
                    )

            if side == "SHORT":
                if high >= levels["stop_loss"]:
                    return self._close(
                        symbol, side, entry_price,
                        levels["stop_loss"], amount, "STOP_LOSS"
                    )

                if low <= levels["take_profit"]:
                    return self._close(
                        symbol, side, entry_price,
                        levels["take_profit"], amount, "TAKE_PROFIT"
                    )

        last_price = future_candles[-1]["close"]
        return self._close(
            symbol, side, entry_price, last_price, amount, "TIME_EXIT"
        )

    def _levels(self, entry_price, side):
        if side == "LONG":
            return {
                "take_profit": entry_price * 1.01,
                "stop_loss": entry_price * 0.995,
            }

        return {
            "take_profit": entry_price * 0.99,
            "stop_loss": entry_price * 1.005,
        }

    def _close(self, symbol, side, entry, exit_price, amount, reason):
        if side == "LONG":
            profit = (exit_price - entry) * amount
            profit_percent = ((exit_price - entry) / entry) * 100
        else:
            profit = (entry - exit_price) * amount
            profit_percent = ((entry - exit_price) / entry) * 100

        return BacktestTrade(
            symbol=symbol,
            side=side,
            entry_price=round(entry, 4),
            exit_price=round(exit_price, 4),
            amount=round(amount, 6),
            profit=round(profit, 2),
            profit_percent=round(profit_percent, 2),
            reason=reason,
        ).to_dict()