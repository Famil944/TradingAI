from backtest.history_loader import HistoryLoader
from backtest.trade_simulator import TradeSimulator
from backtest.backtest_metrics import BacktestMetrics
from backtest.fee_calculator import FeeCalculator
from backtest.analysis_adapter import AnalysisAdapter
from backtest.strategy_runner import StrategyRunner


class ProfessionalBacktest:

    def __init__(self):
        self.loader = HistoryLoader()
        self.simulator = TradeSimulator()
        self.metrics = BacktestMetrics()
        self.fees = FeeCalculator()
        self.adapter = AnalysisAdapter()
        self.strategy = StrategyRunner()

    def run(self, symbol="BTCUSDT", interval="1h", limit=500):
        candles = self.loader.load(symbol, interval, limit)
        trades = []

        for i in range(200, len(candles) - 25):
            history = candles[i - 200:i + 1]
            current = candles[i]
            future = candles[i + 1:i + 25]

            analysis = self.adapter.build(symbol, history)
            strategy_result = self.strategy.analyze(analysis)

            if not strategy_result["approved"]:
                continue

            direction = strategy_result["direction"]

            if direction == "🟢 LONG":
                side = "LONG"
            elif direction == "🔴 SHORT":
                side = "SHORT"
            else:
                continue

            trade = self.simulator.simulate(
                symbol=symbol,
                side=side,
                entry_candle=current,
                future_candles=future,
            )

            entry_notional = trade["entry_price"] * trade["amount"]
            exit_notional = trade["exit_price"] * trade["amount"]
            fee = self.fees.calculate_round_trip(
                entry_notional,
                exit_notional,
            )
            trade["profit"] = round(trade["profit"] - fee, 2)
            trade["fee"] = round(fee, 4)
            trade["quality_score"] = strategy_result["quality"]["score"]
            trade["quality_rating"] = strategy_result["quality"]["rating"]

            trades.append(trade)

        return self.metrics.calculate(trades)


if __name__ == "__main__":
    backtest = ProfessionalBacktest()

    result = backtest.run(
        symbol="BTCUSDT",
        interval="1h",
        limit=500,
    )

    print("📊 Professional Backtest")
    print(f"Сделок: {result['trades']}")
    print(f"Winrate: {result['winrate']}%")
    print(f"Profit Factor: {result['profit_factor']}")
    print(f"Max Drawdown: {result['max_drawdown']}%")
    print(f"Стартовый баланс: {result['start_balance']} USDT")
    print(f"Конечный баланс: {result['end_balance']} USDT")
    print(f"Общая прибыль: {result['total_profit']} USDT")
