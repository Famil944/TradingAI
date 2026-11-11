"""Explicit retrospective source study: no invented historical payouts or platform evidence."""

from collections import defaultdict

from services.otc_conditional import context_setup
from services.otc_strategy import aggregate, normalize_candles

from .data import HORIZONS, Example
from .features import indicators


def build_provider_dataset(rows, *, preview=False):
    groups = defaultdict(list)
    for row in rows:
        if (
            row.get("source") != "otcharts"
            or row.get("venue") != "otc"
            or row.get("is_otc") is not True
        ):
            raise ValueError("Provider provenance required")
        groups[row["symbol"]].append(row)
    horizons = (60, 120, 180, 300, 900) if preview else HORIZONS
    data = {h: [] for h in horizons}
    for symbol, source in groups.items():
        source.sort(key=lambda r: r["time"])
        if any(a["time"] >= b["time"] for a, b in zip(source, source[1:])):
            raise ValueError("Duplicate provider timestamps")
        bars = normalize_candles(source, int(source[-1]["time"] + 60))
        if len(bars) != len(source):
            raise ValueError("Invalid provider bars")
        index = {b.time: i for i, b in enumerate(bars)}
        for i in range(901 if preview else 900, len(bars)):
            entry = bars[i]
            stop = i - 1 if preview else i
            history = bars[max(0, stop - 1200) : stop]
            if any(b.time - a.time != 60 for a, b in zip(history, history[1:])):
                continue
            cached_values = indicators(history) if preview else None
            cached_baseline = (
                context_setup(history, entry.time - 60, 60) if preview else None
            )
            for seconds in horizons:
                end_index = index.get(entry.time + seconds)
                if (
                    entry.time % (60 if preview else seconds)
                    or end_index is None
                    or end_index - i != seconds // 60
                ):
                    continue
                values = (
                    cached_values
                    if preview
                    else indicators(aggregate(history, seconds))
                )
                if values is None:
                    continue
                end = bars[end_index]
                baseline = (
                    cached_baseline
                    if preview
                    else context_setup(history, entry.time, seconds)
                )
                # 90 is a declared reference scenario for comparison, never a historical payout observation.
                data[seconds].append(
                    Example(
                        symbol,
                        entry.time,
                        end.time + 60,
                        values,
                        2
                        if end.open > entry.open
                        else 0
                        if end.open < entry.open
                        else 1,
                        90.0,
                        baseline.direction if baseline else None,
                        entry.open,
                        end.open,
                    )
                )
    for seconds in horizons:
        data[seconds].sort(key=lambda e: (e.at, e.symbol))
    return data, dict(
        input_rows=len(rows),
        retrospective=True,
        reference_payout=90,
        actual_historical_payouts_available=False,
        independent_endpoint_evidence=False,
        **{f"M{h // 60}_retrospective_examples": len(data[h]) for h in horizons},
    )
