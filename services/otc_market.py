"""Validated UTC market events, bounded minute aggregation and settlement."""

import math
from dataclasses import dataclass

from services.otc_strategy import Candle

TIMEFRAMES = (60, 300, 900)


def utc_timestamp(value):
    stamp = float(value)
    if not math.isfinite(stamp) or stamp < 0:
        raise ValueError("Invalid timestamp")
    if stamp >= 100_000_000_000:
        stamp /= 1000
    return stamp


@dataclass(frozen=True)
class Tick:
    symbol: str
    time: float
    price: float


@dataclass(frozen=True)
class Asset:
    symbol: str
    is_otc: bool
    open: bool | None
    payout: float | None
    payout_at: float | None


def eligible(instruments, now, max_age=60):
    return sorted(
        (
            a
            for a in instruments
            if a.is_otc is True
            and a.open is True
            and a.payout is not None
            and math.isfinite(a.payout)
            and 90 <= a.payout <= 100
            and a.payout_at is not None
            and 0 <= now - a.payout_at <= max_age
        ),
        key=lambda a: (-a.payout, a.symbol),
    )


class MinuteBuilder:
    def __init__(self, max_age=8, max_gap=8):
        self.max_age, self.max_gap = max_age, max_gap
        self.last = {}
        self.partial = {}
        self.rejection = None

    def clear(self):
        self.last.clear()
        self.partial.clear()

    def push(self, tick, now):
        self.rejection = None
        if (
            not math.isfinite(tick.price)
            or tick.price <= 0
            or not math.isfinite(tick.time)
            or not 0 <= now - tick.time <= self.max_age
        ):
            self.rejection = "invalid_or_stale_tick"
            return None
        previous = self.last.get(tick.symbol)
        if previous is not None and tick.time <= previous:
            self.rejection = "duplicate_or_unordered_tick"
            return None
        self.last[tick.symbol] = tick.time
        minute = int(tick.time) // 60 * 60
        part = self.partial.get(tick.symbol)
        closed = None
        if part and minute != part[0].time:
            bar, first, last, good = part
            if (
                minute == bar.time + 60
                and first - bar.time <= 2
                and last >= bar.time + 60 - self.max_gap
                and good
                and tick.time - last <= self.max_gap
            ):
                closed = bar
            else:
                self.rejection = "incomplete_candle"
            part = None
        if part is None:
            self.partial[tick.symbol] = (
                Candle(minute, tick.price, tick.price, tick.price, tick.price),
                tick.time,
                tick.time,
                True,
            )
        else:
            bar, first, last, good = part
            self.partial[tick.symbol] = (
                Candle(
                    bar.time,
                    bar.open,
                    max(bar.high, tick.price),
                    min(bar.low, tick.price),
                    tick.price,
                ),
                first,
                tick.time,
                good and tick.time - last <= self.max_gap,
            )
        return closed


def settle(row, tick, tolerance=2):
    if (
        tick.symbol != row["symbol"]
        or not math.isfinite(tick.price)
        or tick.price <= 0
        or not math.isfinite(tick.time)
    ):
        return None
    if tick.time < row["expires_at"]:
        return None
    if (
        not row["synchronized"]
        or row["actual_entry_at"] is None
        or row["entry_price"] is None
        or row["source"] != "otcharts"
        or tick.time - row["expires_at"] > tolerance
        or not 0 <= row["actual_entry_at"] - row["intended_entry_at"] <= tolerance
    ):
        return "UNKNOWN"
    delta = tick.price - row["entry_price"]
    if delta == 0:
        return "DRAW"
    return "WIN" if (delta > 0) == (row["direction"] == "UP") else "LOSS"
