from paper.account import PaperAccount
from paper.trader import PaperTrader
from paper.trade_history import TradeHistory
from paper.statistics import Statistics
from paper.risk_rules import RiskRules
from paper.position_sizer import PositionSizer


class PaperEngine:

    def __init__(self):
        self.account = PaperAccount()
        self.trader = PaperTrader(self.account)
        self.history = TradeHistory()
        self.statistics = Statistics()
        self.risk = RiskRules()
        self.sizer = PositionSizer()
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
            "has_position": self.trader.position is not None,
            "trades": self.history.count(),
            "winrate": self.statistics.winrate()
        }

    def try_open_trade(self, signal_data: dict):
        if not self.enabled or self.trader.position:
            return None

        if signal_data["signal"] != "🟢 LONG":
            return None

        price = signal_data["price"]
        symbol = signal_data["symbol"]
        levels = self.risk.get_levels(price, "LONG")

        balance = self.account.get_balance()
        amount = self.sizer.calculate_amount(
            balance=balance,
            entry_price=price,
            stop_loss=levels["stop_loss"]
        )

        if amount <= 0:
            return None

        success = self.trader.open_long(symbol, price, amount)

        if not success:
            return None

        trade = {
            "symbol": symbol,
            "side": "LONG",
            "entry_price": price,
            "amount": amount,
            "take_profit": levels["take_profit"],
            "stop_loss": levels["stop_loss"],
            "balance": self.account.get_balance()
        }

        self.history.add(trade)
        return trade

    def check_position(self, current_price: float):
        position = self.trader.position

        if not position:
            return None

        reason = self.risk.should_close(position, current_price)

        if not reason:
            return None

        result = self.trader.close_position(current_price)

        if result is None:
            return None

        result["close_reason"] = reason
        self.history.add(result)
        self.statistics.add_trade(result["profit"])

        return result