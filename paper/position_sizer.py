class PositionSizer:

    def __init__(self):
        self.risk_percent = 1.0

    def calculate_amount(
        self,
        balance: float,
        entry_price: float,
        stop_loss: float
    ) -> float:
        risk_money = balance * (self.risk_percent / 100)
        risk_per_unit = abs(entry_price - stop_loss)

        if risk_per_unit <= 0:
            return 0

        amount = risk_money / risk_per_unit
        return round(amount, 6)