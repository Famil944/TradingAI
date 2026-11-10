"""Verified OTC examples with exact minute-open labels and availability times.

No provider requests. Bare candles or synthetic fixtures never prove an edge.
"""

import hashlib
import json
import math
from collections import Counter, defaultdict, deque
from dataclasses import dataclass

from services.otc_market import utc_timestamp
from services.otc_strategy import aggregate, candidate, normalize_candles

from .features import indicators

HORIZONS = (60, 300, 900)


@dataclass(frozen=True)
class Example:
    symbol: str
    at: int
    label_available_at: float
    features: tuple
    label: int  # DOWN=0, DRAW=1, UP=2; DRAW is never silently turned into a loss.
    payout: float
    baseline: str | None
    entry_price: float
    expiry_price: float

    def fingerprint(self):
        return hashlib.sha256(
            json.dumps(self.__dict__, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()


def verified(row):
    try:
        return (
            row.get("source") == "otcharts"
            and row.get("venue") == "otc"
            and row.get("is_otc") is True
            and row.get("verified") is True
            and bool(row.get("verification_evidence"))
            and math.isfinite(utc_timestamp(row["available_at"]))
            and utc_timestamp(row["available_at"]) >= utc_timestamp(row["time"]) + 60
        )
    except (KeyError, ValueError, TypeError, OverflowError):
        return False


def endpoint(row, bar):
    """A precise independent minute-open quote, not a claimed match flag."""
    try:
        return (
            verified(row)
            and bool(row.get("platform_evidence"))
            and utc_timestamp(row["platform_at"]) == bar.time
            and float(row["platform_open"]) == bar.open
        )
    except (KeyError, ValueError, TypeError, OverflowError):
        return False


def payout_known(row, at):
    value = row.get("payout")
    try:
        return (
            row.get("open_confirmed") is True
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and 90 <= value <= 100
            and 0 <= at - utc_timestamp(row["payout_at"]) <= 60
        )
    except (KeyError, ValueError, TypeError, OverflowError):
        return False


def build_dataset(rows):
    grouped = defaultdict(list)
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("symbol"), str)
            or not row.get("symbol")
            or not {"time", "open", "high", "low", "close"}.issubset(row)
        ):
            raise ValueError("Every history row requires a symbol and OHLC fields")
        grouped[row["symbol"]].append(row)
    datasets = {h: [] for h in HORIZONS}
    counts = Counter(input_rows=len(rows))
    for symbol, source in sorted(grouped.items()):
        stamps = [utc_timestamp(r["time"]) for r in source]
        if any(b <= a for a, b in zip(stamps, stamps[1:])):
            raise ValueError("Duplicate or unordered timestamps in history")
        bars = normalize_candles(source, int(stamps[-1] + 60))
        if len(bars) != len(source):
            raise ValueError("Invalid OHLC or candle boundaries in history")
        index_by_time = {bar.time: i for i, bar in enumerate(bars)}
        history = deque(maxlen=1200)
        availability = deque(maxlen=1200)
        for i, (bar, row) in enumerate(zip(bars, source)):
            # Current bar OHLC is never passed to features or the baseline.
            if history and bar.time != history[-1].time + 60:
                history.clear()
                availability.clear()
                counts["history_gaps"] += 1
            if len(history) >= 900:
                for horizon in HORIZONS:
                    if bar.time % horizon:
                        continue
                    counts[f"M{horizon // 60}_opportunities"] += 1
                    if max(availability) > bar.time:
                        counts["features_not_yet_available"] += 1
                        continue
                    if not payout_known(row, bar.time):
                        counts["unknown_or_low_payout_open"] += 1
                        continue
                    target = index_by_time.get(bar.time + horizon)
                    if target is None:
                        counts["unresolved_labels"] += 1
                        continue
                    if target - i != horizon // 60:
                        counts["label_interval_gap"] += 1
                        continue
                    end = bars[target]
                    if not endpoint(row, bar) or not endpoint(source[target], end):
                        counts["unverified_endpoints"] += 1
                        continue
                    values = indicators(aggregate(list(history), horizon))
                    if values is None:
                        counts["insufficient_features"] += 1
                        continue
                    baseline = candidate(list(history), horizon, bar.time)
                    label = (
                        2 if end.open > bar.open else 0 if end.open < bar.open else 1
                    )
                    datasets[horizon].append(
                        Example(
                            symbol,
                            bar.time,
                            max(
                                utc_timestamp(row["available_at"]),
                                utc_timestamp(source[target]["available_at"]),
                            ),
                            values,
                            label,
                            float(row["payout"]),
                            baseline[0] if baseline else None,
                            bar.open,
                            end.open,
                        )
                    )
            # An unverified candle breaks the entire feature prefix.
            if not verified(row):
                history.clear()
                availability.clear()
                counts["unverified_candles"] += 1
            else:
                history.append(bar)
                availability.append(utc_timestamp(row["available_at"]))
    for horizon in HORIZONS:
        datasets[horizon].sort(key=lambda e: (e.at, e.symbol))
        counts[f"M{horizon // 60}_verified_examples"] = len(datasets[horizon])
    return datasets, dict(counts)


def dataset_digest(examples):
    digest = hashlib.sha256()
    for example in examples:
        digest.update(example.fingerprint().encode())
    return digest.hexdigest()
