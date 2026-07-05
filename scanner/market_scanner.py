class MarketScanner:
    def __init__(self, core):
        self.core = core

    def scan_market(self, interval: str = "1h", limit: int = 10, symbols_limit: int = 15):
        results = []

        try:
            symbols = self.core.market.get_top_usdt_symbols(symbols_limit)
        except Exception as e:
            print(f"Ошибка получения списка монет: {e}")
            symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

        fear_greed_data = self.core.fear_greed.get_index()

        for symbol in symbols:
            try:
                data = self.core.analyze_symbol(
                    symbol=symbol,
                    interval=interval,
                    fear_greed_data=fear_greed_data
                )
                results.append(data)
            except Exception as e:
                print(f"Ошибка анализа {symbol}: {e}")

        results.sort(key=lambda x: abs(x["score"]), reverse=True)

        return results[:limit]