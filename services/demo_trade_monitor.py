import time

from services.demo_trade_manager import DemoTradeManager
from services.telegram_notifier import notifier


class DemoTradeMonitor:

    def __init__(
        self,
        symbol,
        entry_price,
        take_profit,
        stop_loss,
        side="LONG",
        interval_seconds=5,
        manager=None,
        on_stop=None,
        stop_order_id=None,
        take_profit_order_id=None,
    ):
        self.symbol = symbol
        self.entry_price = entry_price
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.side = side
        self.interval_seconds = interval_seconds

        self.manager = manager or DemoTradeManager()
        self.on_stop = on_stop
        self.stop_order_id = stop_order_id
        self.take_profit_order_id = take_profit_order_id
        self.running = False

        self.break_even_activated = False
        self.last_trailing_stop = stop_loss

    def start(self):
        self.running = True

        print("👀 Demo Trade Monitor запущен")

        try:
            while self.running:
                result = self.manager.check_tp_sl(
                    symbol=self.symbol,
                    side=self.side,
                    take_profit=self.take_profit,
                    stop_loss=self.stop_loss,
                )

                print(result)

                if result == "Позиции нет.":
                    close_result = (
                        self.manager.finalize_exchange_closed_position(
                            self.symbol
                        )
                    )
                    if close_result:
                        notifier.send(
                            f"🏁 Позиция закрыта биржевым ордером\n\n"
                            f"Монета: {self.symbol}\n"
                            f"Причина: {close_result['close_reason']}\n"
                            f"PnL: {close_result['pnl']} USDT"
                        )
                    self.running = False
                    print("🛑 Demo Trade Monitor остановлен: позиция закрыта.")
                    break

                if "Take Profit" in result or "Stop Loss" in result:
                    notifier.send(
                        f"🔔 Demo-сделка закрыта\n\n"
                        f"Монета: {self.symbol}\n"
                        f"{result}"
                    )

                    self.running = False
                    break

                if not self.break_even_activated:
                    break_even_result = self.manager.check_break_even(
                        symbol=self.symbol,
                        entry_price=self.entry_price,
                        side=self.side,
                    )

                    if (
                        isinstance(break_even_result, dict)
                        and break_even_result.get("move_stop")
                    ):
                        new_stop_loss = float(
                            break_even_result["new_stop_loss"]
                        )

                        stop_should_move = (
                            self.side == "LONG"
                            and new_stop_loss > self.stop_loss
                        ) or (
                            self.side == "SHORT"
                            and new_stop_loss < self.stop_loss
                        )

                        if stop_should_move:
                            stop_order = self.manager.replace_stop_loss(
                                symbol=self.symbol,
                                side=self.side,
                                quantity=self.manager.position_quantity(
                                    self.symbol
                                ),
                                stop_price=new_stop_loss,
                                previous_order_id=self.stop_order_id,
                            )
                            self.stop_order_id = stop_order["orderId"]
                            self.stop_loss = new_stop_loss
                            self.last_trailing_stop = new_stop_loss
                            self.break_even_activated = True

                            print(
                                "🟢 Stop Loss перенесён в безубыток: "
                                f"{self.stop_loss}"
                            )

                            notifier.send(
                                f"🟢 Stop Loss перенесён в безубыток\n\n"
                                f"Монета: {self.symbol}\n"
                                f"Цена входа: {self.entry_price}\n"
                                f"Новый Stop Loss: {self.stop_loss}\n"
                                f"Текущая прибыль: "
                                f"{break_even_result.get('profit_percent', 0)}%"
                            )

                trailing_result = self.manager.calculate_trailing_stop(
                    symbol=self.symbol,
                    entry_price=self.entry_price,
                    current_stop_loss=self.stop_loss,
                    side=self.side,
                )

                if trailing_result.get("updated"):
                    new_stop_loss = float(
                        trailing_result["new_stop_loss"]
                    )

                    trailing_should_move = (
                        self.side == "LONG"
                        and new_stop_loss > self.last_trailing_stop
                    ) or (
                        self.side == "SHORT"
                        and new_stop_loss < self.last_trailing_stop
                    )

                    if trailing_should_move:
                        stop_order = self.manager.replace_stop_loss(
                            symbol=self.symbol,
                            side=self.side,
                            quantity=self.manager.position_quantity(
                                self.symbol
                            ),
                            stop_price=new_stop_loss,
                            previous_order_id=self.stop_order_id,
                        )
                        self.stop_order_id = stop_order["orderId"]
                        self.stop_loss = new_stop_loss
                        self.last_trailing_stop = new_stop_loss

                        print(
                            f"📈 Trailing Stop обновлён: {self.stop_loss}"
                        )

                        notifier.send(
                            f"📈 Trailing Stop обновлён\n\n"
                            f"Монета: {self.symbol}\n"
                            f"Новый Stop Loss: {self.stop_loss}\n"
                            f"Текущая прибыль: "
                            f"{trailing_result.get('profit_percent', 0)}%"
                        )

                time.sleep(self.interval_seconds)
        except Exception as error:
            print(f"DEMO MONITOR ERROR [{self.symbol}]: {error}")
        finally:
            self.running = False
            if self.on_stop is not None:
                self.on_stop(self.symbol, self)

    def stop(self):
        self.running = False
