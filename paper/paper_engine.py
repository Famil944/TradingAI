from paper.account import PaperAccount
from paper.trader import PaperTrader


class PaperEngine:

    def __init__(self):
        self.account = PaperAccount()
        self.trader = PaperTrader(self.account)
        self.enabled = False

    def turn_on(self):
        self.enabled = True
        return "✅ Paper Trading включён"

    def turn_off(self):
        self.enabled = False
        return "⛔ Paper Trading выключен"

    def status(self):
        mode = "включён" if self.enabled else "выключен"

        return {
            "enabled": self.enabled,
            "mode": mode,
            "balance": self.account.get_balance(),
            "has_position": self.trader.position is not None
        }

    def try_open_trade(self, signal_data: dict):
        if not self.enabled:
            return None

        if signal_data["signal"] != "🟢 LONG":
            return None

        price = signal_data["price"]
        symbol = signal_data["symbol"]

        amount = 10 / price

        success = self.trader.open_long(symbol, price, amount)

        if not success:
            return None

        return {
            "symbol": symbol,
            "side": "LONG",
            "entry_price": price,
            "amount": amount,
            "balance": self.account.get_balance()
        }