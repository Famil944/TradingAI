class MarketScanner:
    def __init__(self, core):
        self.core = core

        self.symbols = [
            "BTCUSDT",
            "ETHUSDT",
            "BNBUSDT",
            "SOLUSDT",
            "XRPUSDT",
            "DOGEUSDT",
            "ADAUSDT",
            "AVAXUSDT",
            "LINKUSDT",
            "TONUSDT",
            "TRXUSDT",
            "DOTUSDT",
            "MATICUSDT",
            "LTCUSDT",
            "BCHUSDT",
            "ATOMUSDT",
            "NEARUSDT",
            "APTUSDT",
            "ARBUSDT",
            "OPUSDT",
        ]

    def scan_market(self, interval: str = "1h", limit: int = 10):
        results = []

        for symbol in self.symbols:
            try:
                data = self.core.analyze_symbol(symbol, interval)
                results.append(data)
            except Exception as e:
                print(f"Ошибка анализа {symbol}: {e}")

        results.sort(key=lambda x: abs(x["score"]), reverse=True)

        return results[:limit]