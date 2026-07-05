from paper.account import PaperAccount
from paper.trader import PaperTrader
from paper.trade_history import TradeHistory
from paper.statistics import Statistics
from paper.risk_rules import RiskRules
from paper.position_sizer import PositionSizer
from services.trade_service import TradeService
from paper.trailing_stop import TrailingStop
from paper.partial_take_profit import PartialTakeProfit
from paper.break_even import BreakEven
from paper.event_dispatcher import EventDispatcher
from paper.trade_events import TradeEvent

class PaperEngine:

    def __init__(self):
        self.account = PaperAccount()
        self.trader = PaperTrader(self.account)
        self.history = TradeHistory()
        self.statistics = Statistics()
        self.risk = RiskRules()
        self.sizer = PositionSizer()
        self.trade_service = TradeService()
        self.enabled = False
        self.trailing_stop = TrailingStop()
        self.partial_tp = PartialTakeProfit()
        self.break_even = BreakEven()
        self.dispatcher = EventDispatcher()

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

        signal = signal_data["signal"]

        if signal == "🟢 LONG":
            side = "LONG"
        elif signal == "🔴 SHORT":
            side = "SHORT"
        else:
            return None

        price = signal_data["price"]
        symbol = signal_data["symbol"]
        levels = self.risk.get_levels(price, side)

        balance = self.account.get_balance()
        amount = self.sizer.calculate_amount(
            balance=balance,
            entry_price=price,
            stop_loss=levels["stop_loss"]
        )

        if amount <= 0:
            return None

        if side == "LONG":
           success = self.trader.open_long(
           symbol=symbol,
           price=price,
           amount=amount,
           take_profit=levels["take_profit"],
           stop_loss=levels["stop_loss"]
           )
        else:
           success = self.trader.open_short(
           symbol=symbol,
           price=price,
           amount=amount,
           take_profit=levels["take_profit"],
           stop_loss=levels["stop_loss"]
           )
        
        if not success:
            return None

        trade = {
            "symbol": symbol,
            "side": side,
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
        
        self.trailing_stop.update(position, current_price)
        
        if self.partial_tp.should_take_profit(position, current_price):
           close_amount = self.partial_tp.calculate_close_amount(position)

           profit = position.close_partial(
           close_amount=close_amount,
           exit_price=current_price
           )

        self.account.deposit(
           current_price * close_amount + profit
           )
        self.dispatcher.dispatch(
           TradeEvent(
             TradeEvent.PARTIAL_TAKE_PROFIT,
             {
               "symbol": position.symbol,
               "profit": profit,
               "close_amount": close_amount,
               "price": current_price
             }
           )
        )
        
        if self.break_even.move_to_break_even(position):
          self.dispatcher.dispatch(
            TradeEvent(
              TradeEvent.BREAK_EVEN,
              {
                "symbol": position.symbol,
                "stop_loss": position.stop_loss
              }
            )
        )

        print(
           f"📈 Частичная фиксация прибыли: "
           f"{close_amount} | Прибыль: {round(profit, 2)}"
           )

        reason = self.risk.should_close(position, current_price)

        if not reason:
            return None

        result = self.trader.close_position(current_price)

        if result is None:
            return None

        result["close_reason"] = reason
        self.history.add(result)
        self.statistics.add_trade(result["profit"])
        self.trade_service.save_trade(result)

        return result