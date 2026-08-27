from binance.client import Client


class HistoryLoader:

    def __init__(self):
        self.client = Client()

    def load(self, symbol, interval, limit=1000):
        klines = self.client.get_klines(
            symbol=symbol,
            interval=interval,
            limit=limit,
        )

        candles = []

        for k in klines:
            candles.append({
                "open_time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })

        return candles