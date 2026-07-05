class MarketScanner:
    def __init__(self, core):
        self.core = core

    def scan_market(self, interval: str = "1h", limit: int = 10, symbols_limit: int = 30):
        results = []

        symbols = self.core.market.get_top_usdt_symbols(symbols_limit)

        for symbol in symbols:
            try:
                data = self.core.analyze_symbol(symbol, interval)
                results.append(data)
            except Exception as e:
                print(f"Ошибка анализа {symbol}: {e}")

        results.sort(key=lambda x: abs(x["score"]), reverse=True)

        return results[:limit]