import asyncio
import logging
from datetime import datetime, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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
        trades = self.db.get_manual_trades(status="open")
        if not watches and not trades:
            return
        now = datetime.now(timezone.utc)
        async with BinanceClient() as client:
            for watch in watches:
                await self._check_watch(client, watch, now)
            for trade in trades:
                await self._check_manual_trade(client, trade)

    async def _check_manual_trade(self, client, trade):
        """Продолжает контроль подтверждённой сделки после перезапуска."""
        ticker = await client.get_ticker(trade["symbol"])
        if not ticker:
            return
        price = float(ticker["price"])
        observed_high = price
        observed_low = price
        recovered = []
        try:
            last_checked = datetime.fromisoformat(str(trade["last_checked_at"])).replace(
                tzinfo=timezone.utc
            )
        except (TypeError, ValueError):
            last_checked = datetime.now(timezone.utc)
        offline_seconds = (datetime.now(timezone.utc) - last_checked).total_seconds()
        if offline_seconds > 120:
            if offline_seconds <= 12 * 3600:
                interval, interval_seconds = "1m", 60
            elif offline_seconds <= 3 * 86400:
                interval, interval_seconds = "5m", 300
            elif offline_seconds <= 10 * 86400:
                interval, interval_seconds = "15m", 900
            elif offline_seconds <= 40 * 86400:
                interval, interval_seconds = "1h", 3600
            else:
                interval, interval_seconds = "1d", 86400
            units = offline_seconds / interval_seconds
            candles = await client.get_klines(
                trade["symbol"], interval, limit=min(1000, max(2, int(units) + 2))
            )
            recovered = sorted([
                candle for candle in candles
                if datetime.fromtimestamp(
                    (candle.timestamp / 1000) + interval_seconds,
                    tz=timezone.utc,
                ) > last_checked
            ], key=lambda candle: candle.timestamp)
            if recovered:
                observed_high = max(observed_high, *(candle.high for candle in recovered))
                observed_low = min(observed_low, *(candle.low for candle in recovered))
        updates = {
            "current_price": price,
            "max_price": max(float(trade["max_price"]), observed_high),
            "min_price": min(float(trade["min_price"]), observed_low),
            "last_checked_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        }
        target_price = float(trade["tp1"])
        event = None
        event_time = datetime.now(timezone.utc)
        for candle in recovered:
            target_hit = candle.high >= target_price
            if target_hit:
                event = "TP +3%"
                event_time = datetime.fromtimestamp(
                    candle.timestamp / 1000, tz=timezone.utc
                )
                break
        if event is None:
            if price >= target_price:
                event = "TP +3%"

        dismissed = trade.get("dismissed_reason")
        if event and event == dismissed:
            event = None
        elif not event and dismissed:
            updates["dismissed_reason"] = None

        if event:
            event_price = target_price
            self.db.update_manual_trade(trade["id"], **updates)
            self.db.set_trade_pending(
                trade["id"], event, event_price,
                event_time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✅ Подтверждаю закрытие",
                    callback_data=f"trade_confirm_close:{trade['id']}",
                )],
                [InlineKeyboardButton(
                    text="⏳ Сделка ещё открыта",
                    callback_data=f"trade_keep_open:{trade['id']}",
                )],
            ])
            await self.bot.send_message(
                trade["user_id"],
                f"🎯 Достигнута цель +3%\n\n"
                f"{trade['symbol']} · уровень ${event_price:g}\n"
                "Проверьте Binance и подтвердите фактическое закрытие.",
                reply_markup=keyboard,
            )
            return

        # Просадка отображается только по запросу пользователя в /trades.
        # Фоновый мониторинг продолжает обновлять цену, но не тревожит
        # уведомлениями о критической зоне.
        if trade.get("critical_alerted"):
            updates["critical_alerted"] = 0

        self.db.update_manual_trade(trade["id"], **updates)

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
        if not tp1_hit and price >= tp1:
            self.db.update_watch(watch_id, tp1_hit=1, status="completed")
            await self.bot.send_message(
                user_id,
                f"✅ {symbol}: достигнута цель +3% (${tp1:g})\n"
                f"Цена: ${price:g}. Наблюдение завершено.",
            )
