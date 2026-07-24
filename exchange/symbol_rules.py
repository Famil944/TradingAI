from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    min_notional: Decimal

    @classmethod
    def from_exchange_symbol(cls, data):
        filters = {
            item["filterType"]: item
            for item in data.get("filters", [])
        }
        price_filter = filters["PRICE_FILTER"]
        lot_filter = filters.get("MARKET_LOT_SIZE") or filters["LOT_SIZE"]
        notional_filter = (
            filters.get("MIN_NOTIONAL")
            or filters.get("NOTIONAL")
            or {}
        )
        return cls(
            symbol=data["symbol"],
            tick_size=Decimal(price_filter["tickSize"]),
            step_size=Decimal(lot_filter["stepSize"]),
            min_quantity=Decimal(lot_filter["minQty"]),
            max_quantity=Decimal(lot_filter["maxQty"]),
            min_notional=Decimal(
                notional_filter.get(
                    "notional",
                    notional_filter.get("minNotional", "0"),
                )
            ),
        )

    def normalize_quantity(self, quantity):
        value = Decimal(str(quantity))
        normalized = (value / self.step_size).to_integral_value(
            rounding=ROUND_DOWN
        ) * self.step_size
        if normalized < self.min_quantity or normalized > self.max_quantity:
            raise ValueError(
                f"{self.symbol}: quantity {normalized} вне диапазона "
                f"[{self.min_quantity}, {self.max_quantity}]"
            )
        return normalized

    def normalize_price(self, price, rounding=ROUND_DOWN):
        value = Decimal(str(price))
        return (value / self.tick_size).to_integral_value(
            rounding=rounding
        ) * self.tick_size

    def normalize_stop_price(self, price, side):
        rounding = ROUND_UP if side == "BUY" else ROUND_DOWN
        return self.normalize_price(price, rounding)

    def validate_notional(self, quantity, price):
        notional = Decimal(str(quantity)) * Decimal(str(price))
        if self.min_notional and notional < self.min_notional:
            raise ValueError(
                f"{self.symbol}: notional {notional} меньше "
                f"минимума {self.min_notional}"
            )


def decimal_to_api(value):
    return format(Decimal(value).normalize(), "f")
