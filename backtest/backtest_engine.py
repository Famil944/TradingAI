from backtest.backtest_result import BacktestResult


class BacktestEngine:

    def __init__(self):
        self.result = BacktestResult()

    def run(self, candles, strategy):
        """
        candles - список исторических свечей
        strategy - функция, которая принимает свечу
                   и возвращает результат сделки или None
        """

        for candle in candles:
            trade = strategy(candle)

            if trade is not None:
                self.result.add_trade(trade)

        return self.result.summary()