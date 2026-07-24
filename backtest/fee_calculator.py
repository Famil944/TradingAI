class FeeCalculator:

    def __init__(self, fee_percent=0.04):
        self.fee_percent = fee_percent

    def calculate(self, trade_size):
        return trade_size * (self.fee_percent / 100)

    def calculate_round_trip(self, entry_notional, exit_notional):
        return self.calculate(entry_notional) + self.calculate(exit_notional)
