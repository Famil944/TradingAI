from paper.position import PaperPosition


class PaperTrader:

    def __init__(self, account):
        self.account = account
        self.position = None

    def open_long(self, symbol, price, amount):
        if self.position:
            return False

        cost = price * amount

        if not self.account.withdraw(cost):
            return False

        self.position = PaperPosition(
            symbol=symbol,
            side="LONG",
            entry_price=price,
            amount=amount
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
            "profit": round(profit, 2),
            "balance": self.account.get_balance()
        }

        self.position = None

        return result