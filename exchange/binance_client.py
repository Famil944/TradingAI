from binance.client import Client


class BinanceMarketClient:
    def __init__(self):
        self.client = Client()

    def get_price(self, symbol: str = "BTCUSDT") -> float:
        data = self.client.futures_symbol_ticker(symbol=symbol)
        return float(data["price"])

    def get_klines(self, symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 100):
        return self.client.futures_klines(
            symbol=symbol,
            interval=interval,
            limit=limit
        )

    def get_top_usdt_symbols(self, limit: int = 15):
        tickers = self.client.futures_ticker()

        blocked = [
            "USDCUSDT",
            "FDUSDUSDT",
            "TUSDUSDT",
            "BUSDUSDT",
            "DAIUSDT",
            "UPUSDT",
            "DOWNUSDT",
            "BULLUSDT",
            "BEARUSDT",
        ]

        symbols = []

        for ticker in tickers:
            symbol = ticker.get("symbol", "")

            if not symbol.endswith("USDT"):
                continue

            if any(bad in symbol for bad in blocked):
                continue

            try:
                quote_volume = float(ticker.get("quoteVolume", 0))
            except Exception:
                quote_volume = 0

            symbols.append({
                "symbol": symbol,
                "quote_volume": quote_volume
            })

        symbols.sort(key=lambda x: x["quote_volume"], reverse=True)

        return [item["symbol"] for item in symbols[:limit]]