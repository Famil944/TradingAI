from backtest.history_loader import HistoryLoader
from backtest.trade_simulator import TradeSimulator
from backtest.backtest_result import BacktestResult
from backtest.simple_strategy import SimpleStrategy


symbol = "BTCUSDT"
interval = "1h"

loader = HistoryLoader()
strategy = SimpleStrategy()
simulator = TradeSimulator()
result = BacktestResult()

candles = loader.load(symbol, interval, limit=500)

for i in range(len(candles) - 25):
    candle = candles[i]
    future_candles = candles[i + 1:i + 25]

    signal = strategy.get_signal(candle)

    if signal:
        trade = simulator.simulate(
            symbol=symbol,
            side=signal,
            entry_candle=candle,
            future_candles=future_candles,
        )

        result.add_trade(trade)

print("📊 Backtest Result")
print(result.summary())