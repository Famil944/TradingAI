"""Fixed prospective hypotheses. Never a production selection or promotion gate."""

import math
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from statistics import mean, pstdev

from services.otc_selection_quality import not_extended, primary_rejection
from services.otc_strategy import Candle, aggregate, ema

VERSION = "bollinger-cycles-study-v4"
HOURLY_VERSION = "hourly-alignment-study-v1"


def extended_history(minutes, path, row):
    """Use only archived data actually available when this decision was made."""
    if path is None or not Path(path).exists():
        return minutes
    with closing(
        sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5
        )
    ) as db:
        rows = db.execute(
            "SELECT time,open,high,low,close FROM bars WHERE symbol=? AND time+60<=? "
            "AND first_observed<=? AND last_observed<=? AND revisions=0 "
            "AND source='otcharts_history' ORDER BY time DESC LIMIT 2400",
            (
                row["symbol"],
                row["feature_asof"],
                row["observed_at"],
                row["observed_at"],
            ),
        ).fetchall()
    merged = {b.time: b for b in minutes if b.time + 60 <= row["feature_asof"]}
    merged.update({r[0]: Candle(*r) for r in rows})
    return [merged[t] for t in sorted(merged)][-2400:]


def higher_trend(minutes, period, asof):
    if period not in (900, 1800, 3600):
        raise ValueError("Unsupported higher timeframe")
    past = [b for b in minutes if b.time + 60 <= asof]
    if period == 900:
        bars = aggregate(past, 900)
    else:
        # M30/H1 require every constituent M15 candle.
        halves = {b.time: b for b in aggregate(past, 900)}
        bars = []
        for at, first in sorted(halves.items()):
            group = [halves.get(at + offset) for offset in range(0, period, 900)]
            if at % period == 0 and all(b is not None for b in group):
                bars.append(
                    Candle(
                        at,
                        first.open,
                        max(b.high for b in group),
                        min(b.low for b in group),
                        group[-1].close,
                    )
                )
    bars = bars[-30:]
    if (
        len(bars) < 30
        or not 0 <= asof - bars[-1].time - period < period
        or any(b.time - a.time != period for a, b in zip(bars, bars[1:]))
    ):
        return dict(period=period, state="unknown", available=False)
    prices = [b.close for b in bars]
    spread = ema(prices, 8) - ema(prices, 20)
    threshold = mean(b.high - b.low for b in bars[-20:]) * 0.1
    state = "UP" if spread > threshold else "DOWN" if spread < -threshold else "neutral"
    return dict(
        period=period,
        state=state,
        available=True,
        spread=spread,
        threshold=threshold,
        last_closed=asdict(bars[-1]),
    )


def pattern_directions(minutes, seconds, asof):
    """Independent forecasts from closed bars; no future or incomplete candles."""
    period = 60 if seconds == 120 else seconds
    bars = aggregate([b for b in minutes if b.time + 60 <= asof], period)
    bars = bars[-20:]
    if (
        len(bars) < 20
        or not 0 <= asof - bars[-1].time - period < period
        or any(b.time - a.time != period for a, b in zip(bars, bars[1:]))
        or any(
            not all(
                math.isfinite(v) and v > 0 for v in (b.open, b.high, b.low, b.close)
            )
            or not b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
            for b in bars
        )
    ):
        return {}
    closes = [b.close for b in bars]
    middle, deviation = mean(closes), pstdev(closes)
    result = {}
    if deviation > 0:
        lower, upper = middle - 2 * deviation, middle + 2 * deviation
        bottom, top = bars[-1].low <= lower, bars[-1].high >= upper
        # Both bands touched is ambiguous, so do not pick a direction.
        if bottom != top:
            result["bollinger_reversal"] = dict(
                direction="UP" if bottom else "DOWN",
                lower=lower,
                upper=upper,
                candle=asdict(bars[-1]),
                analysis_period=period,
            )
    if seconds == 120:
        colors = [
            1 if b.close > b.open else -1 if b.close < b.open else 0 for b in bars[-4:]
        ]
        if all(colors) and all(a == -b for a, b in zip(colors, colors[1:])):
            result["alternating_four"] = dict(
                direction="UP" if colors[-1] == -1 else "DOWN",
                colors=colors,
                analysis_period=60,
                last_closed=bars[-1].time,
            )
    return result


def prepare(records, histories, archive_path=None):
    """Only the M5 intersection and three-attempt Bollinger cycles remain active."""
    prepared = []
    for original in records:
        row = dict(original)
        if row.get("reason") != "eligible" or not row.get("gates", {}).get(
            "min_confirmation"
        ):
            prepared.append(row)
            continue
        seconds = row["expires_at"] - row["entry_at"]
        masks = {}
        if seconds == 300:
            evidence = row.get("evidence") or {}
            masks["not_extended"] = not_extended(
                evidence.get("features") or {},
                evidence.get("confirmations", {}).get("market_context") or {},
                row["direction"],
            )
            masks["strict_primary"] = (
                primary_rejection(row.get("evidence") or {}, seconds, row["direction"])
                is None
            )
        row["experiment"] = dict(version=VERSION, masks=masks, shadow_only=True)
        prepared.append(row)
        from services.otc_community_patterns import forecasts, route

        decisions = forecasts(
            histories.get(row["symbol"], []), seconds, row["feature_asof"]
        )
        state = (
            (row.get("evidence") or {})
            .get("confirmations", {})
            .get("market_state", {})
            .get("state", "unknown")
        )
        routed = route(decisions, state)
        if routed:
            decisions["regime_router"] = routed
        for name, decision in decisions.items():
            prepared.append(
                dict(
                    row,
                    version=name + "-shadow-v1",
                    direction=decision["direction"],
                    selected=False,
                    evidence={"pattern": decision},
                    experiment=dict(
                        version=VERSION,
                        shadow_only=True,
                        pattern=name,
                        masks={name: True},
                    ),
                )
            )
        forecast = pattern_directions(
            histories.get(row["symbol"], []), seconds, row["feature_asof"]
        ).get("bollinger_reversal")
        if forecast:
            prepared.append(
                dict(
                    row,
                    version="bollinger-three-attempts-v1",
                    direction=forecast["direction"],
                    selected=False,
                    expires_at=row["entry_at"] + 3 * seconds,
                    evidence={"pattern": forecast},
                    experiment=dict(
                        version=VERSION,
                        shadow_only=True,
                        masks={},
                        cycle_seconds=seconds,
                        attempts=3,
                        same_direction=True,
                    ),
                )
            )
    return prepared
