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

    def get_top_usdt_symbols(self, limit: int = 30):
        tickers = self.client.get_ticker()
        usdt_pairs = []

        for ticker in tickers:
            symbol = ticker.get("symbol", "")

            if not symbol.endswith("USDT"):
                continue

            if any(x in symbol for x in ["UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"]):
                continue

            quote_volume = float(ticker.get("quoteVolume", 0))

            usdt_pairs.append({
                "symbol": symbol,
                "quote_volume": quote_volume
            })

        usdt_pairs.sort(key=lambda x: x["quote_volume"], reverse=True)

        return [item["symbol"] for item in usdt_pairs[:limit]]