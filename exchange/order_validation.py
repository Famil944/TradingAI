import math
import re


SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,20}$")
VALID_SIDES = {"BUY", "SELL"}


def validate_order(symbol, side, quantity):
    normalized_symbol = str(symbol).upper()
    normalized_side = str(side).upper()

    if not SYMBOL_PATTERN.fullmatch(normalized_symbol):
        raise ValueError(f"Некорректный символ: {symbol!r}")
    if normalized_side not in VALID_SIDES:
        raise ValueError(f"Некорректная сторона ордера: {side!r}")

    numeric_quantity = float(quantity)
    if not math.isfinite(numeric_quantity) or numeric_quantity <= 0:
        raise ValueError("Количество должно быть конечным числом больше нуля")

    return normalized_symbol, normalized_side, numeric_quantity
