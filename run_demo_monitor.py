from exchange.binance_testnet_client import BinanceTestnetClient
from services.demo_trade_manager import DemoTradeManager
from services.demo_trade_monitor import DemoTradeMonitor


def main():
    symbol = "BTCUSDT"
    quantity = 0.001
    client = BinanceTestnetClient()
    manager = DemoTradeManager()
    price = float(client.price(symbol)["price"])
    take_profit = round(price * 1.01, 1)
    stop_loss = round(price * 0.99, 1)

    print("🤖 Demo Monitor Bot")
    print(f"Вход: {price}")
    print(f"TP: {take_profit}")
    print(f"SL: {stop_loss}")

    manager.open_long(symbol, quantity)
    monitor = DemoTradeMonitor(
        symbol=symbol,
        entry_price=price,
        take_profit=take_profit,
        stop_loss=stop_loss,
        interval_seconds=5,
    )
    monitor.start()


if __name__ == "__main__":
    main()
