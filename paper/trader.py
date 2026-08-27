from paper.position import PaperPosition


class PaperTrader:

    def __init__(self, account):
        self.account = account
        self.position = None

    def open_long(self, symbol, price, amount, take_profit=None, stop_loss=None):
        return self._open_position(symbol, "LONG", price, amount, take_profit, stop_loss)

    def open_short(self, symbol, price, amount, take_profit=None, stop_loss=None):
        return self._open_position(symbol, "SHORT", price, amount, take_profit, stop_loss)

    def _open_position(self, symbol, side, price, amount, take_profit, stop_loss):
        if self.position:
            return False

        cost = price * amount

        if not self.account.withdraw(cost):
            return False

        self.position = PaperPosition(
            symbol=symbol,
            side=side,
            entry_price=price,
            amount=amount,
            take_profit=take_profit,
            stop_loss=stop_loss
        )

        return True

    def close_position(self, price):
        if not self.position:
            return None

        profit = self.position.close(price)

        self.account.deposit(
            self.position.entry_price * self.position.amount + profit
        )

        result = {
            "symbol": self.position.symbol,
            "side": self.position.side,
            "entry_price": self.position.entry_price,
            "exit_price": price,
            "profit": round(profit, 2),
            "balance": self.account.get_balance()
        }

        self.position = None

        return result