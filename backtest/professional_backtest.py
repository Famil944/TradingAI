from backtest.history_loader import HistoryLoader
from backtest.trade_simulator import TradeSimulator
from backtest.backtest_metrics import BacktestMetrics
from backtest.fee_calculator import FeeCalculator


class ProfessionalBacktest:

    def __init__(self):
        self.loader = HistoryLoader()
        self.simulator = TradeSimulator()
        self.metrics = BacktestMetrics()
        self.fees = FeeCalculator()

    def run(self, symbol="BTCUSDT", interval="1h", limit=500):
        candles = self.loader.load(symbol, interval, limit)
        trades = []

        for i in range(50, len(candles) - 25):
            current = candles[i]
            previous = candles[i - 1]
            future = candles[i + 1:i + 25]

            signal = self._get_signal(previous, current)

            if not signal:
                continue

            trade = self.simulator.simulate(
                symbol=symbol,
                side=signal,
                entry_candle=current,
                future_candles=future,
            )

            fee = self.fees.calculate(100)
            trade["profit"] = round(trade["profit"] - fee, 2)
            trade["fee"] = round(fee, 4)

            trades.append(trade)

        return self.metrics.calculate(trades)

    def _get_signal(self, previous, current):
        if current["close"] > previous["close"]:
            return "LONG"

        if current["close"] < previous["close"]:
            return "SHORT"

        return None


if name == "main":
    backtest = ProfessionalBacktest()
    result = backtest.run(
        symbol="BTCUSDT",
        interval="1h",
        limit=500
    )

    print("📊 Professional Backtest")
    print(result)