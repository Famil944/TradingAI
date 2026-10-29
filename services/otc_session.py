"""Timed Pocket Option OTC signal sessions; no trade execution."""
import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from services.otc_data import usage, symbols, candles
from services.otc_stream import ticks
from services.otc_strategy import Candle, candidate, normalize_candles


logger = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")
TIMEFRAMES = (60, 300, 900)


def eligible(instruments):
    return sorted(
        (item for item in instruments
         if item.is_otc is True and item.open is not False
         and item.payout is not None and item.payout >= 90),
        key=lambda i: (-i.payout, i.symbol),
    )


class OTCSession:
    def __init__(self):
        self.task = None
        self.chat_id = None
        self.timeframes = TIMEFRAMES
        self.ends_at = None
        self.phase = "остановлен"
        self.monitored = 0
        self.available = 0

    def running(self):
        return self.task is not None and not self.task.done()

    def start(self, bot, chat_id, minutes, timeframes):
        if self.running():
            return False
        self.chat_id = chat_id
        self.timeframes = tuple(timeframes)
        self.ends_at = time.time() + minutes * 60 if minutes else None
        self.task = asyncio.create_task(self._run(bot))
        return True

    def stop(self):
        if not self.running():
            return False
        self.task.cancel()
        return True

    def status(self):
        end = (
            datetime.fromtimestamp(self.ends_at, MOSCOW).strftime("%H:%M МСК")
            if self.ends_at else "до остановки"
        )
        tf = ", ".join(f"M{x // 60}" for x in self.timeframes)
        return (
            f"OTC: {self.phase}\nЭкспирации: {tf}\n"
            f"До: {end}\n"
            f"Отслеживается: {self.monitored} из {self.available} активов 90%+."
        )

    async def _run(self, bot):
        try:
            self.phase = "проверка доступа"
            current = await usage()
            if current.streams_limit <= 0:
                await bot.send_message(
                    self.chat_id,
                    "⛔ Бесплатный OTCharts не поддерживает потоковые котировки "
                    "и выплаты. Сигналы в реальном времени не запущены. "
                    "Команда /otc_status покажет состояние."
                )
                return
            if current.remaining <= 0:
                await bot.send_message(self.chat_id, "⛔ Исчерпан лимит OTCharts.")
                return
            self.phase = "получение списка OTC"
            assets = eligible(await symbols())
            self.available = len(assets)
            cap = current.instruments_per_stream or len(assets)
            chosen = assets[:cap]
            self.monitored = len(chosen)
            if not chosen:
                await bot.send_message(
                    self.chat_id, "Нет открытых OTC с подтверждённой выплатой 90%+."
                )
                return
            names = ", ".join(item.symbol for item in chosen)
            await bot.send_message(
                self.chat_id,
                f"▶️ OTC-сканер: {len(chosen)} из {len(assets)} активов 90%+.\n"
                f"{names}\n"
                "На тарифах с лимитом потоков остальная часть рынка "
                "НЕ отслеживается одновременно.\n"
                "Сигналы возможны только на свежих закрытых свечах."
            )
            self.phase = "загрузка истории"
            history = {}
            for asset in chosen:
                try:
                    rows = await candles(asset.symbol)
                    history[asset.symbol] = normalize_candles(
                        [vars(row) for row in rows], int(time.time())
                    )
                except Exception:
                    logger.warning("OTC historical data unavailable: %s", asset.symbol)
            if not history:
                await bot.send_message(self.chat_id, "⛔ Нет истории свечей для анализа.")
                return
            self.phase = "поток активен"
            await self._scan(bot, history, {asset.symbol: asset for asset in chosen})
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("OTC session error: %s", type(exc).__name__)
            await bot.send_message(
                self.chat_id,
                f"⛔ OTC-сканер остановлен: {type(exc).__name__}. "
                "Проверь ключ, доступ и лимиты через /otc_status."
            )
        finally:
            self.phase = "остановлен"
            self.monitored = 0
            self.available = 0

    async def _scan(self, bot, history, chosen):
        partial = {}
        last_refresh = time.monotonic()
        active = dict(chosen)
        while self.ends_at is None or time.time() < self.ends_at:
            try:
                async for tick in ticks(list(chosen)):
                    if self.ends_at is not None and time.time() >= self.ends_at:
                        return
                    # Both Pocket Option bars and ticks use venue UTC+2.
                    now = time.time()
                    stamp = int(tick.time) - 7200
                    if abs(now - stamp) > 8 or tick.price <= 0:
                        continue
                    minute = stamp // 60 * 60
                    previous = partial.get(tick.symbol)
                    if previous and minute < previous[0].time:
                        continue
                    if previous and minute > previous[0].time:
                        bar, first_tick, last_tick = previous
                        if (minute - bar.time == 60 and first_tick - bar.time <= 2
                                and bar.time + 55 <= last_tick):
                            values = history[tick.symbol]
                            if not values or values[-1].time < bar.time:
                                values.append(bar)
                                del values[:-1200]
                            if tick.symbol in active and stamp - minute <= 5:
                                for seconds in self.timeframes:
                                    if minute % seconds:
                                        continue
                                    result = candidate(values, seconds, minute)
                                    if result:
                                        direction, why = result
                                        entry = datetime.fromtimestamp(stamp, MOSCOW)
                                        await bot.send_message(
                                            self.chat_id,
                                            f"{'🟢' if direction == 'UP' else '🔴'} "
                                            f"{tick.symbol} · {'ВВЕРХ' if direction == 'UP' else 'ВНИЗ'}\n"
                                            f"Экспирация: M{seconds // 60}\n"
                                            f"Выплата по каталогу: {active[tick.symbol].payout:g}%\n"
                                            f"Наблюдаемая цена: {tick.price:g}\n"
                                            f"Время: {entry:%H:%M:%S} МСК\n"
                                            f"Причина: {why}\n"
                                            "⚠️ Не является гарантией выигрыша. "
                                            "Фактическая цена входа может отличаться."
                                        )
                        previous = None
                    if previous is None:
                        bar = Candle(minute, tick.price, tick.price, tick.price, tick.price)
                        partial[tick.symbol] = (bar, stamp, stamp)
                    else:
                        bar, first, _ = previous
                        partial[tick.symbol] = (
                            Candle(bar.time, bar.open, max(bar.high, tick.price),
                                   min(bar.low, tick.price), tick.price),
                            first, stamp,
                        )
                    if time.monotonic() - last_refresh > 900:
                        last_refresh = time.monotonic()
                        try:
                            updated = {a.symbol: a for a in eligible(await symbols())}
                            active = {s: updated[s] for s in chosen if s in updated}
                            self.available = len(updated)
                        except Exception:
                            # Payout data stale -> no signals until verified again.
                            active = {}
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("OTC stream disconnected, retrying after delay")
                await asyncio.sleep(65)
