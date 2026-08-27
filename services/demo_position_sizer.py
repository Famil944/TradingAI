class DemoPositionSizer:

    def __init__(
        self,
        risk_percent=1,
        max_quantity=0.01,
        max_balance_percent=20,
    ):
        if not 0 < risk_percent <= 5:
            raise ValueError("Риск на сделку должен быть в диапазоне (0, 5]")
        if max_quantity is not None and max_quantity <= 0:
            raise ValueError("Максимальное количество должно быть больше нуля")
        if not 0 < max_balance_percent <= 25:
            raise ValueError("Доля баланса должна быть в диапазоне (0, 25]")
        self.risk_percent = risk_percent
        self.max_quantity = max_quantity
        self.max_balance_percent = max_balance_percent

    def adaptive_risk_percent(self, quality_score=0):
        """Scale risk conservatively; configured risk is always the ceiling."""
        score = float(quality_score or 0)
        if score >= 85:
            factor = 1.0
        elif score >= 75:
            factor = 0.75
        else:
            factor = 0.5
        return max(0.1, min(self.risk_percent, self.risk_percent * factor))

    def _quantity(self, balance, entry_price, risk_per_unit, quality_score):
        if risk_per_unit <= 0 or entry_price <= 0:
            return 0
        risk_percent = self.adaptive_risk_percent(quality_score)
        risk_quantity = (
            balance * (risk_percent / 100)
        ) / risk_per_unit
        allocation_quantity = (
            balance * (self.max_balance_percent / 100)
        ) / entry_price
        quantity = min(risk_quantity, allocation_quantity)
        if self.max_quantity is not None:
            quantity = min(quantity, self.max_quantity)
        return round(quantity, 8)

    def calculate_long_quantity(
        self,
        balance,
        entry_price,
        stop_loss,
        quality_score=100,
    ):
        risk_per_unit = entry_price - stop_loss
        return self._quantity(
            balance, entry_price, risk_per_unit, quality_score
        )

    def calculate_short_quantity(
        self,
        balance,
        entry_price,
        stop_loss,
        quality_score=100,
    ):
        risk_per_unit = stop_loss - entry_price
        return self._quantity(
            balance, entry_price, risk_per_unit, quality_score
        )
