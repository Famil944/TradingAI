import asyncio
import logging
from datetime import datetime, timezone

from database.db import Database
from exchange.binance_client import BinanceClient


logger = logging.getLogger(__name__)


class SignalWatchService:
    """Tracks user-selected signals and sends state-change notifications."""

    def __init__(self, bot, interval_seconds: int = 30, news_service=None):
        self.bot = bot
        self.db = Database()
        self.interval_seconds = interval_seconds
        self.news_service = news_service
        self._news_risk_alerted = set()
        self._stop_event = asyncio.Event()

    async def run(self):
        self._stop_event.clear()
        logger.info("Signal watch service started")
        while not self._stop_event.is_set():
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Signal watch iteration failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    def stop(self):
        self._stop_event.set()

    async def check_once(self):
        watches = self.db.get_active_watches()
        if not watches:
            return
        now = datetime.now(timezone.utc)
        async with BinanceClient() as client:
            for watch in watches:
                await self._check_watch(client, watch, now)

    async def _check_watch(self, client, watch, now):
        (
            watch_id, user_id, _signal_id, symbol, entry_min, entry_max,
            tp1, tp2, tp3, tp4, stop_loss, expires_at, entered,
            tp1_hit, tp2_hit, tp3_hit, tp4_hit, _stop_hit, _status, *_rest,
        ) = watch
        expiry = datetime.fromisoformat(str(expires_at)).replace(tzinfo=timezone.utc)
        if now >= expiry:
            self.db.update_watch(watch_id, status="expired")
            await self.bot.send_message(
                user_id, f"⌛ {symbol}: срок сигнала истёк. Наблюдение завершено."
            )
            return

        ticker = await client.get_ticker(symbol)
        if not ticker:
            return
        price = float(ticker["price"])

        if self.news_service and watch_id not in self._news_risk_alerted:
            news = await self.news_service.assess(symbol)
            if news.available and (news.critical_risk or news.score <= -40):
                self._news_risk_alerted.add(watch_id)
                heading = "🔴 СИГНАЛ ВЫХОДА" if entered else "⛔ ВХОД ОТМЕНЁН"
                action = (
                    "Рекомендуется закрыть позицию или существенно снизить риск."
                    if entered else "Не входить: обнаружен критический рыночный риск."
                )
                await self.bot.send_message(
                    user_id,
                    f"{heading}\n\n{symbol}\nЦена: ${price:g}\n"
                    f"Причина: критический новостной риск.\n{action}",
                )
                if not entered:
                    self.db.update_watch(watch_id, status="cancelled_by_risk")
                    return

        if not entered and entry_min <= price <= entry_max:
            entered = 1
            self.db.update_watch(watch_id, entered=1)
            await self.bot.send_message(
                user_id,
                f"🎯 {symbol} вошёл в зону входа\n"
                f"Цена: ${price:g}\nЗона: ${entry_min:g}–${entry_max:g}",
            )

        if not entered:
            return
        if price <= stop_loss:
            self.db.update_watch(watch_id, stop_hit=1, status="stopped")
            await self.bot.send_message(
                user_id, f"🛑 {symbol}: достигнут Stop ${stop_loss:g}. Наблюдение завершено."
            )
            return

        targets = (("TP1", tp1, tp1_hit), ("TP2", tp2, tp2_hit),
                   ("TP3", tp3, tp3_hit), ("TP4", tp4, tp4_hit))
        for index, (label, target, was_hit) in enumerate(targets, 1):
            if not was_hit and price >= target:
                field = f"tp{index}_hit"
                updates = {field: 1}
                if index == 4:
                    updates["status"] = "completed"
                self.db.update_watch(watch_id, **updates)
                await self.bot.send_message(
                    user_id, f"✅ {symbol}: достигнут {label} ${target:g}\nЦена: ${price:g}"
                )
