import asyncio
import logging

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import settings
from database.db import Database
from services.action_decision_service import ActionDecisionService


logger = logging.getLogger(__name__)


class AutoSignalService:
    """Periodically scans the market and notifies users about strong signals."""

    def __init__(self, bot, scanner, scan_lock, news_service):
        self.bot = bot
        self.scanner = scanner
        self.scan_lock = scan_lock
        self.db = Database()
        self.news_service = news_service
        self.decision_service = ActionDecisionService()
        self._stop_event = asyncio.Event()

    async def run(self):
        self._stop_event.clear()
        interval = max(1, settings.scan_interval_minutes) * 60
        logger.info(
            "Auto signal service started: interval=%s min, min_score=%s",
            settings.scan_interval_minutes,
            settings.auto_notify_min_score,
        )
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            try:
                await self.scan_and_notify()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Automatic signal scan failed")

    def stop(self):
        self._stop_event.set()

    async def scan_and_notify(self):
        if self.scan_lock.locked():
            logger.info("Automatic scan skipped: manual scan is active")
            return
        async with self.scan_lock:
            results = await self.scanner.scan_market(
                top_limit=settings.auto_priority_top_limit,
                respect_cooldown=True,
            )
        decisions = []
        for item in results:
            signal = item["signal_object"]
            news = await self.news_service.assess(signal.symbol)
            decision = self.decision_service.decide(signal.score, news)
            if decision.action in {"BUY", "EARLY_BUY", "AVOID"}:
                decisions.append((item, decision))
        if not decisions:
            logger.info("Automatic TOP-%s scan completed without actions", settings.auto_priority_top_limit)
            return
        users = self.db.get_notification_user_ids()
        decisions.sort(key=lambda pair: pair[1].priority, reverse=True)
        for item, decision in decisions[:3]:
            signal = item["signal_object"]
            is_buy = decision.action in {"BUY", "EARLY_BUY"}
            keyboard = None
            if is_buy:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🔔 Следить до TP/Stop",
                        callback_data=f"auto_watch:{item['signal_id']}",
                    )
                ]])
            heading = {
                "BUY": "🟢 ПОКУПКА",
                "EARLY_BUY": "🟠 РАННИЙ ВХОД",
                "AVOID": "⛔ НЕ ПОКУПАТЬ",
            }[decision.action]
            text = (
                f"{heading}\n\n"
                f"{signal.symbol} · Score {signal.score}/100\n"
                f"Цена: ${signal.current_price:g}\n"
                f"Вход: ${signal.entry_zone_min:g}–${signal.entry_zone_max:g}\n"
                f"TP1: ${signal.targets.tp1:g} · TP2: ${signal.targets.tp2:g}\n"
                f"Stop: ${signal.stop_loss:g}\n"
                f"R/R: {signal.risk_reward:.2f}\n"
                f"Приоритет: {decision.priority}/100\n"
                f"Рыночный фон: {decision.news_label}"
            )
            for user_id in users:
                try:
                    await self.bot.send_message(user_id, text, reply_markup=keyboard)
                except Exception:
                    logger.exception("Cannot notify Telegram user %s", user_id)
