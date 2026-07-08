class FeeCalculator:

    def __init__(self, fee_percent=0.04):
        self.fee_percent = fee_percent

    def calculate(self, trade_size):
        return trade_size * (self.fee_percent / 100)