from exchange.binance_testnet_client import BinanceTestnetClient


class DemoTradeManager:

    def __init__(self):
        self.client = BinanceTestnetClient()

    def open_long(self, symbol, quantity):
        print("🚀 Открываем LONG...")

        order = self.client.market_order(
            symbol=symbol,
            side="BUY",
            quantity=quantity,
        )

        print("✅ LONG открыт")

        return order
    def check_break_even(self, symbol, entry_price, trigger_percent=0.5):
        positions = self.client.open_positions(symbol)

        if not positions:
            return "Позиции нет."

        price = float(self.client.price(symbol)["price"])
        position = positions[0]
        amount = float(position["positionAmt"])

        if amount > 0:
            profit_percent = ((price - entry_price) / entry_price) * 100

            if profit_percent >= trigger_percent:
                return {
                    "move_stop": True,
                    "new_stop_loss": entry_price,
                    "profit_percent": round(profit_percent, 2),
                }

        return {
            "move_stop": False,
            "profit_percent": 0,
        }
    def calculate_trailing_stop(
        self,
        symbol,
        entry_price,
        current_stop_loss,
        trigger_percent=0.5,
        distance_percent=0.3,
    ):
        positions = self.client.open_positions(symbol)

        if not positions:
            return {
                "updated": False,
                "reason": "Позиции нет.",
            }

        price = float(self.client.price(symbol)["price"])
        position = positions[0]
        amount = float(position["positionAmt"])

        if amount > 0:
            profit_percent = ((price - entry_price) / entry_price) * 100

            if profit_percent < trigger_percent:
                return {
                    "updated": False,
                    "reason": "Прибыль ещё мала для trailing stop.",
                    "profit_percent": round(profit_percent, 2),
                }

            new_stop_loss = round(price * (1 - distance_percent / 100), 1)

            if new_stop_loss > current_stop_loss:
                return {
                    "updated": True,
                    "new_stop_loss": new_stop_loss,
                    "profit_percent": round(profit_percent, 2),
                }

        return {
            "updated": False,
            "reason": "Trailing stop не обновлён.",
        }

    def close_position(self, symbol):
        positions = self.client.open_positions(symbol)

        if not positions:
            print("Открытых позиций нет.")
            return None

        position = positions[0]
        amount = float(position["positionAmt"])

        side = "BUY" if amount > 0 else "SELL"
        quantity = abs(amount)

        result = self.client.close_position_market(
            symbol=symbol,
            side=side,
            quantity=quantity,
        )

        self.client.cancel_all_orders(symbol)

        print("✅ Позиция закрыта")
        return result

    def check_tp_sl(self, symbol, take_profit, stop_loss):
        positions = self.client.open_positions(symbol)

        if not positions:
            return "Позиции нет."

        price = float(self.client.price(symbol)["price"])

        position = positions[0]
        amount = float(position["positionAmt"])

        print(f"Цена: {price}")
        print(f"TP: {take_profit}")
        print(f"SL: {stop_loss}")

        if amount > 0:
            if price >= take_profit:
                self.close_position(symbol)
                return "🎯 Take Profit сработал"

            if price <= stop_loss:
                self.close_position(symbol)
                return "🛑 Stop Loss сработал"

        return "⏳ Позиция удерживается"