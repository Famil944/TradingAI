"""Extra context checks only 10–15 seconds before entry; never cancel retroactively."""

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from services.otc_conditional import context_setup, display_symbol
from services.otc_session import db_call

logger = logging.getLogger(__name__)


def analyze(row, snapshot, now):
    known_at = int(now) // 60 * 60
    evidence = {"known_asof": known_at, "future_condition_checked": False}
    if snapshot is None:
        return "CANCELLED", "no_fresh_data", evidence
    bars, asset = snapshot
    if (
        asset is None
        or asset.observed_at is None
        or not 0 <= now - asset.observed_at <= 60
    ):
        return "CANCELLED", "no_fresh_market_metadata", evidence
    evidence["market"] = asdict(asset)
    if asset.open is not True:
        return "CANCELLED", "instrument_closed", evidence
    # An issued plan is evaluated by its price outcome. Payout observations
    # remain in evidence, but a changed/unknown payout never cancels the plan.
    evidence["signal_payout"] = row.get("payout")
    evidence["payout_affects_outcome"] = False
    current = context_setup(bars, known_at, int(row["expires_at"] - row["entry_at"]))
    if current is None:
        return "CANCELLED", "context_not_confirmed", evidence
    evidence["context"] = current.evidence
    if current.direction != row["direction"]:
        return "CANCELLED", "direction_changed", evidence
    return "KEEP", "context_confirmed", evidence


def message(row, decision, reason):
    clock = datetime.fromtimestamp(row["entry_at"], ZoneInfo("Europe/Moscow")).strftime(
        "%H:%M:%S"
    )
    title = f"🔵 Основной · {display_symbol(row['symbol'])} · M{int(row['expires_at'] - row['entry_at']) // 60} · {clock} МСК\n"
    if decision == "KEEP":
        return title + "План в силе. Вход только при исходном условии свечи."
    label = {
        "no_fresh_data": "нет свежих данных",
        "no_fresh_market_metadata": "нет свежих данных о выплате",
        "instrument_closed": "инструмент закрыт",
        "context_not_confirmed": "доп. анализ не подтвердил контекст",
        "direction_changed": "направление изменилось",
    }.get(reason, "дополнительная проверка")
    return title + "ОТМЕНА — " + label


class PreflightChecks:
    def __init__(self, db, bot, chat_id, snapshot, clock=time.time):
        self.db, self.bot, self.chat_id, self.snapshot, self.clock = (
            db,
            bot,
            chat_id,
            snapshot,
            clock,
        )

    async def check(self, row):
        now = self.clock()
        if not 10 <= row["entry_at"] - now <= 15:
            return
        snapshot = self.snapshot(row["symbol"])
        decision, reason, evidence = await asyncio.to_thread(
            analyze, row, snapshot, now
        )
        if not await db_call(
            self.db.claim_preflight, row["id"], self.clock(), decision, reason, evidence
        ):
            return
        remaining = row["entry_at"] - self.clock() - 10
        if remaining <= 0:
            await db_call(self.db.complete_preflight, row["id"], self.clock(), False)
            return
        try:
            async with asyncio.timeout(min(2.5, remaining)):
                await self.bot.send_message(
                    self.chat_id, message(row, decision, reason)
                )
        except (Exception, asyncio.CancelledError):
            await db_call(self.db.complete_preflight, row["id"], self.clock(), False)
            return
        await db_call(self.db.complete_preflight, row["id"], self.clock(), True)

    async def tick(self):
        rows = await db_call(self.db.preflight_targets, self.clock())
        await asyncio.gather(*(self.check(row) for row in rows))

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                logger.warning("Pre-entry context check: %s", type(exc).__name__)
            await asyncio.sleep(0.25)
