"""Condition-first virtual assessment on provider OHLC; never a real trade record."""

import asyncio
import logging
import math
import time
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from database.db import Database
from services.otc_conditional import Plan, Setup, condition_met, display_symbol
from services.otc_data import OTChartsProvider
from services.otc_session import db_call
from services.otc_strategy import normalize_candles

logger = logging.getLogger(__name__)
LABELS = {
    "WIN": "WIN ✅",
    "LOSS": "LOSS ❌",
    "DRAW": "НИЧЬЯ",
    "SKIPPED": "ПРОПУСК",
    "UNKNOWN": "НЕИЗВЕСТНО",
    "CANCELLED": "ОТМЕНЁН",
    "NOT_DELIVERED": "НЕ ДОСТАВЛЕН",
}


def assess(row, bars, now):
    def answer(outcome, condition, reason, **extra):
        return dict(outcome=outcome, condition=condition, reason=reason, **extra)

    if row["lifecycle"] == "CANCELLED":
        return answer("CANCELLED", "UNKNOWN", "cancelled_before_entry")
    if row["delivery"] in ("FAILED", "LATE"):
        return answer("NOT_DELIVERED", "UNKNOWN", "delivery_not_on_time")
    if row["delivery"] != "SENT":
        return answer("UNKNOWN", "UNKNOWN", "delivery_unconfirmed")
    if now < row["expires_at"] + 60:
        return answer("UNKNOWN", "UNKNOWN", "endpoint_bar_not_closed")
    valid = normalize_candles([asdict(b) for b in bars], int(now))
    by_time = {b.time: b for b in valid}
    condition = by_time.get(int(row["condition_from"]))
    if condition is None:
        return answer("UNKNOWN", "UNKNOWN", "missing_condition_candle")
    plan = Plan(
        row["symbol"],
        row["setup_at"],
        row["entry_at"],
        row["expires_at"],
        row["condition_from"],
        row["delivery_deadline"],
        Setup(row["direction"], row["colour"], row["invalidation"], row["reason"]),
    )
    evidence = {
        "condition": asdict(condition),
        "rule": "condition-first-exact-minute-opens-v1",
    }
    if not condition_met(plan, condition):
        return answer(
            "SKIPPED", "NOT_MET", "colour_or_level_not_met", evidence=evidence
        )
    entry = by_time.get(int(row["entry_at"]))
    expiry = by_time.get(int(row["expires_at"]))
    if entry is None or expiry is None:
        return answer("UNKNOWN", "MET", "missing_boundary_price", evidence=evidence)
    if not all(math.isfinite(v) and v > 0 for v in (entry.open, expiry.open)):
        return answer("UNKNOWN", "MET", "invalid_price", evidence=evidence)
    delta = expiry.open - entry.open
    outcome = (
        "DRAW"
        if delta == 0
        else "WIN"
        if (delta > 0) == (row["direction"] == "UP")
        else "LOSS"
    )
    evidence.update(entry=asdict(entry), expiry=asdict(expiry))
    return answer(
        outcome,
        "MET",
        "provider_minute_open_comparison",
        entry_price=entry.open,
        expiry_price=expiry.open,
        evidence=evidence,
    )


def format_stats(rows, title):
    if not rows:
        return f"{title}: оценённых планов пока нет."
    lines = [f"🧪 {title} · виртуально по OTCharts"]
    for row in rows:
        if len(rows) > 1:
            direction = "ВВЕРХ" if row.get("direction") == "UP" else "ВНИЗ"
            colour = "зелёная" if row.get("colour") == "green" else "красная"
            version = (
                "v2"
                if row.get("strategy", "").startswith("conditional-context-v2")
                else "v1"
            )
            lines.append(f"{version}: {colour} → {direction}")
        wins = row["wins"] or 0
        losses = row["losses"] or 0
        n = wins + losses
        percent = f"{100 * wins / n:.1f}%" if n else "—"
        lines.append(f"Успешность {percent} (n={n}): WIN {wins}, LOSS {losses}")
        lines.append(
            f"Пропуск {row['skipped'] or 0} · ничья {row['draws'] or 0} · неизвестно {row['unknown'] or 0}"
        )
        lines.append(
            f"Ожидают {row['pending'] or 0} · отменены {row['cancelled'] or 0} · не доставлены {row['not_delivered'] or 0}"
        )
    return "\n".join(lines)


class Evaluator:
    def __init__(self, db, provider, bot, chat_id, observer_path=None, clock=time.time):
        self.db = db
        self.provider = provider
        self.bot = bot
        self.chat_id = chat_id
        self.observer_path = observer_path
        self.observer_db = None
        self.clock = clock
        self.cache = {}
        self.last_fetch = {}

    async def tick(self):
        now = self.clock()
        journals = [(self.db, "Бот")]
        if self.observer_path is not None and self.observer_path.exists():
            if self.observer_db is None:
                self.observer_db = Database(self.observer_path)
                await db_call(self.observer_db.init_db)
            journals.append((self.observer_db, "Наблюдатель"))
        for db, title in journals:
            rows = await db_call(db.assessment_targets, now)
            for row in rows:
                bars = []
                if row["delivery"] == "SENT" and row["lifecycle"] != "CANCELLED":
                    symbol = row["symbol"]
                    if now - self.last_fetch.get(symbol, float("-inf")) >= 60:
                        self.last_fetch[symbol] = now
                        try:
                            plan = await self.provider.usage()
                            if plan.remaining <= 10:
                                raise RuntimeError("quota reserve")
                            self.cache[symbol] = await self.provider.candles(
                                symbol, self.clock()
                            )
                        except Exception as exc:
                            logger.warning(
                                "Virtual evaluation data: %s", type(exc).__name__
                            )
                            if now < row["expires_at"] + 180:
                                continue
                    bars = self.cache.get(symbol, [])
                result = assess(row, bars, now)
                if (
                    result["outcome"] == "UNKNOWN"
                    and row["delivery"] == "SENT"
                    and now < row["expires_at"] + 180
                ):
                    continue
                await db_call(db.assess_plan, row["id"], result, self.clock())
            notifications = await db_call(db.assessment_notifications, now - 900)
            if notifications:
                text = (
                    "🧪 "
                    + title
                    + " · виртуально OTCharts\n"
                    + "\n".join(
                        display_symbol(r["symbol"])
                        + " "
                        + datetime.fromtimestamp(
                            r["entry_at"], ZoneInfo("Europe/Moscow")
                        ).strftime("%H:%M")
                        + ": "
                        + LABELS[r["outcome"]]
                        for r in notifications
                    )
                )
                # Do not repeatedly send after an ambiguous Telegram failure.
                await db_call(
                    db.assessment_notified, [r["plan_id"] for r in notifications], now
                )
                try:
                    async with asyncio.timeout(10):
                        await self.bot.send_message(self.chat_id, text)
                except Exception as exc:
                    logger.warning(
                        "Virtual result notification: %s", type(exc).__name__
                    )

    async def run(self):
        try:
            while True:
                try:
                    await self.tick()
                except Exception as exc:
                    logger.warning("Virtual evaluator: %s", type(exc).__name__)
                await asyncio.sleep(15)
        finally:
            await self.provider.close()


def configured(db, bot, settings, root):
    return Evaluator(
        db,
        OTChartsProvider(settings.otcharts_api_key),
        bot,
        settings.admin_chat_id,
        root / "data" / "observer.db",
    )
