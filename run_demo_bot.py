from services.demo_trade_manager import DemoTradeManager
from exchange.binance_testnet_client import BinanceTestnetClient

symbol = "BTCUSDT"
quantity = 0.001

client = BinanceTestnetClient()
manager = DemoTradeManager()

price = float(client.price(symbol)["price"])

take_profit = round(price * 1.01, 1)
stop_loss = round(price * 0.99, 1)

print("🤖 Demo Trade Bot")
print(f"Цена входа: {price}")
print(f"TP: {take_profit}")
print(f"SL: {stop_loss}")

manager.open_long(symbol, quantity)

result = manager.check_tp_sl(
    symbol=symbol,
    take_profit=take_profit,
    stop_loss=stop_loss,
)

print(result)