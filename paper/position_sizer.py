class PositionSizer:

    def __init__(self):
        self.risk_percent = 1.0
        self.max_position_percent = 10.0

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

        risk_amount = risk_money / risk_per_unit

        max_position_money = balance * (self.max_position_percent / 100)
        max_amount = max_position_money / entry_price

        amount = min(risk_amount, max_amount)

        return round(amount, 6)