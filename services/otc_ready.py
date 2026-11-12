"""Experimental primary signals from already completed candles, no future-colour condition."""

import asyncio
import time

from services.otc_conditional import context_setup
from services.otc_groq import fresh
from services.otc_ml.preview import MLPreview

VERSION = "primary-ready-v5"


def select_ready(histories, assets, quotes, now, seconds, end=None):
    entry = int(now) // 60 * 60 + 60
    if seconds not in (60, 300, 900) or not 15 <= entry - now <= 20:
        return None
    if end and entry + seconds + 60 > end:
        return None
    known_at = int(now) // 60 * 60
    candidates = []
    for symbol, bars in histories.items():
        asset = assets.get(symbol)
        if not fresh(asset, quotes.get(symbol), now):
            continue
        past = [bar for bar in bars if bar.time + 60 <= known_at]
        setup = context_setup(past, known_at, seconds)
        if setup is None:
            continue
        features = setup.evidence["features"]
        atr = features["atr14"]
        if (
            atr <= 0
            or features["adx14"] < 0.20
            or abs(features["ema20_gap"]) / atr > 1.5
        ):
            continue
        if setup.direction == "UP":
            aligned = (
                features["macd_hist"] > 0
                and features["rsi14"] <= 0.68
                and features["bb_position"] <= 0.9
            )
        else:
            aligned = (
                features["macd_hist"] < 0
                and features["rsi14"] >= 0.32
                and features["bb_position"] >= 0.1
            )
        if not aligned:
            continue
        score = (
            features["adx14"]
            + 0.05 * abs(features["ema_spread"]) / atr
            - 0.05 * abs(features["ema20_gap"]) / atr
        )
        candidates.append(
            (
                score,
                symbol,
                dict(
                    symbol=symbol,
                    name=asset.name or symbol,
                    entry_at=entry,
                    expires_at=entry + seconds,
                    direction=setup.direction,
                    payout=asset.payout,
                    model_sha256=VERSION,
                    model_role="unqualified_primary_experiment",
                    feature_asof=known_at,
                    evidence=setup.evidence,
                    condition_required=False,
                    quality_score=score,
                ),
            )
        )
    return (
        max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None
    )


class ReadyPrimary(MLPreview):
    label = "Основной · готовый"
    icon = "🔵"

    def __init__(self, journal, clock=time.time, settlement_provider=None):
        super().__init__(journal, None, clock, settlement_provider)
        self.allowed_timeframes = (60, 300, 900)
        self.sent_count = 0

    async def plan(self, bot, chat_id, snapshot):
        history, assets, quotes, end = snapshot
        histories = {s: list(b) for s, b in history.items()}
        pending = await asyncio.to_thread(self.journal.pending)
        blocked = {int(p["expires_at"] - p["entry_at"]) for p in pending}
        for seconds in self.allowed_timeframes:
            now = self.clock()
            if (
                seconds in blocked
                or not self.enabled
                or not 15 <= (int(now) // 60 * 60 + 60) - now <= 20
            ):
                continue
            # Two-minute spacing for M1 remains; M5/M15 wait for settlement.
            if seconds == 60:
                import sqlite3

                with sqlite3.connect(self.journal.path) as db:
                    previous = db.execute(
                        "SELECT max(entry_at) FROM predictions WHERE expires_at-entry_at=60 AND sent_at IS NOT NULL"
                    ).fetchone()[0]
                if previous is not None and int(now) // 60 * 60 + 60 - previous < 120:
                    continue
            selected = await asyncio.to_thread(
                select_ready, histories, dict(assets), dict(quotes), now, seconds, end
            )
            if not selected or not self.enabled or not self.snapshot:
                continue
            _, current_assets, current_quotes, current_end = self.snapshot
            asset = current_assets.get(selected["symbol"])
            if not fresh(asset, current_quotes.get(selected["symbol"]), self.clock()):
                continue
            if current_end and selected["expires_at"] + 60 > current_end:
                continue
            selected["payout"] = asset.payout
            if await self.publish(selected, bot, chat_id):
                self.sent_count += 1
