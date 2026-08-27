class PositionCalculator:

    def calculate(self, position, current_price):
        if position.side == "LONG":
            profit = (current_price - position.entry_price) * position.amount
            profit_percent = (
                (current_price - position.entry_price)
                / position.entry_price
            ) * 100

            distance_to_tp = (
                (position.take_profit - current_price)
                / current_price
            ) * 100

            distance_to_sl = (
                (current_price - position.stop_loss)
                / current_price
            ) * 100

        else:
            profit = (position.entry_price - current_price) * position.amount
            profit_percent = (
                (position.entry_price - current_price)
                / position.entry_price
            ) * 100

            distance_to_tp = (
                (current_price - position.take_profit)
                / current_price
            ) * 100

            distance_to_sl = (
                (position.stop_loss - current_price)
                / current_price
            ) * 100

        return {
            "profit": round(profit, 2),
            "profit_percent": round(profit_percent, 2),
            "distance_to_tp": round(distance_to_tp, 2),
            "distance_to_sl": round(distance_to_sl, 2)
        }