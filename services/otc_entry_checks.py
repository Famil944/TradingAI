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


def message(row, decision, now, evidence=None):
    evidence = evidence or {}
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
        market = evidence.get("entry_market")
        if market and market.get("open") is False:
            return prefix + "Проверка: инструмент закрыт"
        if market and market.get("payout") is not None and market["payout"] < 90:
            return prefix + f"Проверка: выплата {market['payout']:g}% ниже 90%"
        return (
            prefix
            + "Дополнительная проверка не подтвердила вход; исходный план учитывается отдельно"
        )
    if decision == "LATE":
        return (
            prefix
            + f"Условие выполнено · проверка +{max(0, now - row['entry_at']):.1f} с. Сигнал учитывается."
        )
    if decision == "NO_DATA":
        return prefix + "Проверка недоступна · исходный план не отменён"
    if now - row["entry_at"] > 5:
        return (
            prefix
            + f"Условие выполнено · проверка +{now - row['entry_at']:.1f} с. Сигнал учитывается."
        )
    direction = "ВВЕРХ" if row["direction"] == "UP" else "ВНИЗ"
    return (
        prefix
        + f"Условие выполнено по OTCharts · {direction}. Сверь платформу; время входа не переносится."
    )


class EntryChecks:
    def __init__(
        self,
        db,
        provider,
        bot,
        chat_id,
        clock=time.time,
        require_quality=True,
        ml=None,
        source_snapshot=None,
    ):
        self.db, self.provider, self.bot, self.chat_id = db, provider, bot, chat_id
        self.clock = clock
        self.require_quality = require_quality
        self.ml = ml
        self.source_snapshot = source_snapshot
        self.fetched = {}
        self.cache = {}

    async def tick(self):
        rows = await db_call(self.db.entry_check_targets, self.clock())
        for row in rows:
            now = self.clock()
            bars = []
            live_asset = None
            if self.source_snapshot is not None and row["lifecycle"] != "CANCELLED":
                snapshot = self.source_snapshot(row["symbol"])
                if snapshot:
                    live_bars, live_asset = snapshot
                    if decide(row, live_bars, now)[0] is not None:
                        bars = live_bars
                    elif now - row["entry_at"] < 3:
                        continue
            if (
                not bars
                and row["lifecycle"] != "CANCELLED"
                and now - row["entry_at"] < 30
            ):
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
                evidence["context_advisory_only"] = not passed
                try:
                    if (
                        live_asset is not None
                        and live_asset.observed_at is not None
                        and 0 <= self.clock() - live_asset.observed_at <= 60
                    ):
                        assets = [live_asset]
                    else:
                        assets = await self.provider.symbols()
                    asset = next((a for a in assets if a.symbol == row["symbol"]), None)
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
                    elif self.ml is not None:
                        try:
                            async with asyncio.timeout(0.25):
                                ml_result = await asyncio.to_thread(
                                    self.ml.inspect, row, bars, asset.payout
                                )
                            ml_result["predicted_at"] = self.clock()
                            evidence["xgboost"] = ml_result
                            if self.ml.requires_filter(row) and not ml_result.get(
                                "live"
                            ):
                                decision = "NO_DATA"
                            if ml_result.get("live") and not ml_result.get("accept"):
                                decision = "REJECTED"
                        except Exception as exc:
                            evidence["xgboost"] = {
                                "status": "unavailable",
                                "error_type": type(exc).__name__,
                                "live": False,
                            }
                            if self.ml.requires_filter(row):
                                decision = "NO_DATA"
                except Exception:
                    decision = "NO_DATA"
            evidence["confirmation_delay_seconds"] = self.clock() - row["entry_at"]
            evidence["confirmation_is_advisory"] = True
            if decision is None:
                continue
            evidence["quality_version"] = 3
            # Claim before network send: ambiguous Telegram failures must not duplicate advice.
            decision = await db_call(
                self.db.claim_entry_check, row["id"], self.clock(), decision, evidence
            )
            if not decision:
                continue
            try:
                async with asyncio.timeout(5):
                    await self.bot.send_message(
                        self.chat_id, message(row, decision, self.clock(), evidence)
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
