import time

from services.demo_trade_manager import DemoTradeManager


class DemoTradeMonitor:

    def __init__(
        self,
        symbol,
        entry_price,
        take_profit,
        stop_loss,
        interval_seconds=5,
    ):
        self.symbol = symbol
        self.entry_price = entry_price
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.interval_seconds = interval_seconds
        self.manager = DemoTradeManager()
        self.running = False

    def start(self):
        self.running = True

        print("👀 Demo Trade Monitor запущен")

        while self.running:
            result = self.manager.check_tp_sl(
                symbol=self.symbol,
                take_profit=self.take_profit,
                stop_loss=self.stop_loss,
            )

            print(result)

            if "Take Profit" in result or "Stop Loss" in result:
                self.running = False
                break

            be = self.manager.check_break_even(
                symbol=self.symbol,
                entry_price=self.entry_price,
            )

            if isinstance(be, dict) and be.get("move_stop"):
                self.stop_loss = be["new_stop_loss"]
                print(f"🟢 Stop Loss перенесён в безубыток: {self.stop_loss}")

            trailing = self.manager.calculate_trailing_stop(
                symbol=self.symbol,
                entry_price=self.entry_price,
                current_stop_loss=self.stop_loss,
            )

            if trailing.get("updated"):
                self.stop_loss = trailing["new_stop_loss"]
                print(f"📈 Trailing Stop обновлён: {self.stop_loss}")

            time.sleep(self.interval_seconds)

    def stop(self):
        self.running = False