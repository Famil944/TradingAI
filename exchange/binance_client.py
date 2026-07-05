from binance.client import Client


class BinanceMarketClient:
    def __init__(self):
        self.client = Client()

    def get_price(self, symbol: str = "BTCUSDT") -> float:
        data = self.client.get_symbol_ticker(symbol=symbol)
        return float(data["price"])

    def get_klines(self, symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 100):
        return self.client.get_klines(
            symbol=symbol,
            interval=interval,
            limit=limit
        )