"""Chronological OHLC replay and fixed-strategy forward validation.

Input JSONL: time (UTC minute open, seconds or milliseconds), open/high/low/close,
symbol, payout, payout_at (UTC), open_confirmed. Independent platform_open,
platform_at and platform_evidence are required for a resolved hypothetical result.
The next observed bar's OPEN is a hypothetical entry, never its high/low/close.
No tuning or parameter selection is performed on validation folds.
"""

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

from services.otc_market import utc_timestamp
from services.otc_statistics import metrics
from services.otc_strategy import candidate, normalize_candles


def matched_open(raw, bar):
    """Evidence means a recorded independent quote, not just a truthy flag."""
    try:
        price = float(raw["platform_open"])
        stamp = utc_timestamp(raw["platform_at"])
        return (
            bool(raw.get("platform_evidence"))
            and math.isfinite(price)
            and price > 0
            and price == bar.open
            and abs(stamp - bar.time) <= 2
        )
    except (KeyError, TypeError, ValueError):
        return False


def replay(rows, timeframe=60, warmup=900, fold_size=240, cooldown=900):
    if timeframe not in (60, 300, 900) or warmup < 1 or fold_size < 1:
        raise ValueError("Invalid replay configuration")
    groups = defaultdict(list)
    for row in rows:
        groups[str(row["symbol"])].append(row)
    outcomes = []
    folds = []
    for symbol, source in sorted(groups.items()):
        # Require chronological, unique data: replay must never silently reorder evidence.
        stamps = [utc_timestamp(r["time"]) for r in source]
        if any(b <= a for a, b in zip(stamps, stamps[1:])):
            raise ValueError(
                "Replay requires strictly increasing timestamps per symbol"
            )
        bars = normalize_candles(source, int(stamps[-1] + 60)) if stamps else []
        if len(bars) != len(source):
            raise ValueError("Replay contains invalid candles")
        pending = []
        previous_signal = float("-inf")
        for index, (bar, raw) in enumerate(zip(bars, source)):
            # This event reveals only the new bar's opening price to the decision.
            # The complete current bar is not passed into candidate().
            next_pending = []
            for result in pending:
                if bar.time >= result["expires_at"]:
                    if (
                        bar.time != result["expires_at"]
                        or not result["evidence"]
                        or not matched_open(raw, bar)
                    ):
                        result["outcome"] = "UNKNOWN"
                    else:
                        difference = bar.open - result["entry_price"]
                        result["outcome"] = (
                            "DRAW"
                            if difference == 0
                            else "WIN"
                            if (difference > 0) == (result["direction"] == "UP")
                            else "LOSS"
                        )
                    result["expiry_price"] = bar.open
                    outcomes.append(result)
                else:
                    next_pending.append(result)
            pending = next_pending
            if (
                index < warmup
                or bar.time % timeframe
                or bar.time - previous_signal < cooldown
            ):
                continue
            # Every complete historical candle given to the strategy ends by this event.
            result = candidate(bars[max(0, index - 1200) : index], timeframe, bar.time)
            if result is None:
                continue
            payout = raw.get("payout")
            stamp = raw.get("payout_at")
            if (
                raw.get("open_confirmed") is not True
                or not isinstance(payout, (int, float))
                or isinstance(payout, bool)
                or not math.isfinite(payout)
                or not 90 <= payout <= 100
                or stamp is None
                or not 0 <= bar.time - utc_timestamp(stamp) <= 60
            ):
                continue
            previous_signal = bar.time
            pending.append(
                dict(
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy="ema-rsi-atr-v1",
                    signal_at=bar.time,
                    entry_price=bar.open,
                    expires_at=bar.time + timeframe,
                    direction=result[0],
                    reason=result[1],
                    payout=payout,
                    evidence=raw.get("platform_evidence")
                    if matched_open(raw, bar)
                    else None,
                    outcome="UNKNOWN",
                    fold=(index - warmup) // fold_size,
                )
            )
        outcomes.extend(pending)
        count = max(0, math.ceil((len(bars) - warmup) / fold_size))
        for fold in range(count):
            start = warmup + fold * fold_size
            end = min(start + fold_size, len(bars))
            subset = [
                r for r in outcomes if r["symbol"] == symbol and r["fold"] == fold
            ]
            folds.append(
                dict(
                    symbol=symbol,
                    fold=fold,
                    history_bars=start,
                    evaluation_from=bars[start].time,
                    evaluation_to=bars[end - 1].time,
                    **metrics(sorted(subset, key=lambda r: r["signal_at"])),
                )
            )
    outcomes.sort(key=lambda r: (r["signal_at"], r["symbol"]))
    return dict(
        mode="hypothetical OHLC replay; no real trade entries",
        validation="expanding history / chronological held-out folds; fixed parameters, no tuning",
        strategy="ema-rsi-atr-v1",
        metrics=metrics(outcomes),
        folds=folds,
        signals=outcomes,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeframe", type=int, choices=(60, 300, 900), default=60)
    parser.add_argument("--warmup", type=int, default=900)
    parser.add_argument("--fold-size", type=int, default=240)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("Output must differ from input")
    with args.input.open(encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    report = replay(rows, args.timeframe, args.warmup, args.fold_size)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print("Replay report:", args.output)


if __name__ == "__main__":
    main()
