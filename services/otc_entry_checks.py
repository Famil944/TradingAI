"""Post-close condition notification; late data never authorizes a backdated entry."""

import asyncio
import logging
import math
import time
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from services.otc_conditional import (
    Plan,
    Setup,
    condition_met,
    context_setup,
    display_symbol,
)
from services.otc_session import db_call
from services.otc_strategy import aggregate, normalize_candles

logger = logging.getLogger(__name__)


def decide(row, bars, now):
    if row["lifecycle"] == "CANCELLED":
        return "CANCELLED", {}
    if now < row["entry_at"]:
        return None, {}
    seconds = int(row["entry_at"] - row["condition_from"])
    if seconds not in (60, 300, 900):
        return "NO_DATA", {}
    valid = normalize_candles([asdict(b) for b in bars], int(now))
    candle = next(
        (b for b in aggregate(valid, seconds) if b.time == row["condition_from"]), None
    )
    if candle is None:
        return ("NO_DATA" if now - row["entry_at"] >= 30 else None), {}
    plan = Plan(
        row["symbol"],
        row["setup_at"],
        row["entry_at"],
        row["expires_at"],
        row["condition_from"],
        row["delivery_deadline"],
        Setup(row["direction"], row["colour"], row["invalidation"], row["reason"]),
    )
    return ("MET" if condition_met(plan, candle) else "SKIPPED"), {
        "condition": asdict(candle),
        "source": "otcharts",
    }


def entry_quality(row, bars):
    seconds = int(row["expires_at"] - row["entry_at"])
    current = context_setup(bars, int(row["entry_at"]), seconds)
    if current is None:
        return False, {"reason": "context_no_longer_valid"}
    if current.direction != row["direction"]:
        return False, {"reason": "direction_changed"}
    return True, {"reason": "context_revalidated", "context": current.evidence}


def message(row, decision, now):
    name = display_symbol(row["symbol"])
    minutes = int(row["expires_at"] - row["entry_at"]) // 60
    clock = datetime.fromtimestamp(row["entry_at"], ZoneInfo("Europe/Moscow")).strftime(
        "%H:%M:%S"
    )
    prefix = f"{name} · M{minutes} · {clock} МСК\n"
    if decision == "CANCELLED":
        return prefix + "ОТМЕНА"
    if decision == "SKIPPED":
        return prefix + "ПРОПУСК — условие не выполнено"
    if decision == "REJECTED":
        return prefix + "ПРОПУСК — контекст или выплата изменились"
    if decision == "LATE":
        return prefix + "ПРОПУСК — подтверждение опоздало"
    if decision == "NO_DATA":
        return prefix + "ПРОПУСК — нет подтверждённых данных"
    delay = max(0, int(now - row["entry_at"]))
    if delay > 5:
        return (
            prefix
            + f"Условие выполнено по OTCharts. Задержка {delay} с — вход прошёл, ПРОПУСК."
        )
    direction = "ВВЕРХ" if row["direction"] == "UP" else "ВНИЗ"
    return (
        prefix
        + f"Условие выполнено по OTCharts · {direction}. Сверь платформу; время входа не переносится."
    )


class EntryChecks:
    def __init__(
        self, db, provider, bot, chat_id, clock=time.time, require_quality=True
    ):
        self.db, self.provider, self.bot, self.chat_id = db, provider, bot, chat_id
        self.clock = clock
        self.require_quality = require_quality
        self.fetched = {}
        self.cache = {}

    async def tick(self):
        rows = await db_call(self.db.entry_check_targets, self.clock())
        for row in rows:
            now = self.clock()
            bars = []
            if row["lifecycle"] != "CANCELLED" and now - row["entry_at"] < 30:
                symbol = row["symbol"]
                if now - self.fetched.get(symbol, float("-inf")) >= 10:
                    self.fetched[symbol] = now
                    try:
                        plan = await self.provider.usage()
                        if plan.remaining <= 10:
                            continue
                        self.cache[symbol] = await self.provider.candles(
                            symbol, self.clock()
                        )
                    except Exception as exc:
                        logger.warning("Entry check data: %s", type(exc).__name__)
                        continue
                bars = self.cache.get(symbol, [])
            decision, evidence = decide(row, bars, self.clock())
            if row["symbol"] in getattr(self.provider, "closed_symbols", set()):
                decision, evidence = "CANCELLED", {"reason": "instrument_closed"}
            if decision == "MET" and self.require_quality:
                passed, details = entry_quality(row, bars)
                evidence["entry_quality"] = details
                if not passed:
                    decision = "REJECTED"
                else:
                    try:
                        assets = await self.provider.symbols()
                        asset = next(
                            (a for a in assets if a.symbol == row["symbol"]), None
                        )
                        fresh = (
                            asset is not None
                            and asset.open is True
                            and asset.payout is not None
                            and math.isfinite(asset.payout)
                            and 90 <= asset.payout <= 100
                            and asset.observed_at is not None
                            and 0 <= self.clock() - asset.observed_at <= 60
                        )
                        evidence["entry_market"] = (
                            asdict(asset) if asset is not None else None
                        )
                        if not fresh:
                            decision = "REJECTED"
                    except Exception:
                        decision = "NO_DATA"
            if decision == "MET" and self.clock() - row["entry_at"] > 5:
                decision = "LATE"
            if decision is None:
                continue
            evidence["quality_version"] = 2
            # Claim before network send: ambiguous Telegram failures must not duplicate advice.
            decision = await db_call(
                self.db.claim_entry_check, row["id"], self.clock(), decision, evidence
            )
            if not decision:
                continue
            try:
                async with asyncio.timeout(5):
                    await self.bot.send_message(
                        self.chat_id, message(row, decision, self.clock())
                    )
            except Exception as exc:
                logger.warning("Entry check notification: %s", type(exc).__name__)

    async def run(self):
        try:
            while True:
                try:
                    await self.tick()
                except Exception as exc:
                    logger.warning("Entry checks: %s", type(exc).__name__)
                await asyncio.sleep(1)
        finally:
            await self.provider.close()
