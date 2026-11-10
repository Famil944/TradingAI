"""Single-owner research sessions. No trading or broker credentials."""

import asyncio
import contextlib
import logging
import math
import time
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from services.otc_data import AccessUnavailable, OTChartsProvider
from services.otc_market import TIMEFRAMES, MinuteBuilder, Tick, eligible, settle
from services.otc_strategy import candidate, normalize_candles
from services.otc_stream import reconnect_delay

logger = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")


async def db_call(function, *args, **kwargs):
    """Drain SQLite work on cancellation before shutdown or another session."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class OTCSession:
    def __init__(self, db=None, provider=None, config=None, clock=time.time):
        if config is None:
            from config.settings import settings

            config = settings
        self.config = config
        self.db = db
        self.provider = provider or OTChartsProvider(config.otcharts_api_key)
        self.clock = clock
        self.task = None
        self.chat_id = None
        self.timeframes = TIMEFRAMES
        self.ends_at = None
        self.phase = "остановлен"
        self.monitored = 0
        self.available = 0
        self.last_tick = None
        self.last_error = None
        self._lock = asyncio.Lock()
        self._last_events = {}
        self._quotes = {}
        self.plan = None
        self.transport = "live"
        self.poll_requests_used = 0

    @property
    def test_mode(self):
        return bool(getattr(self.config, "test_mode", False))

    def _eligible(self, instruments, now, max_age):
        return eligible(instruments, now, max_age, research=self.test_mode)

    def _price_max_age(self):
        return (
            getattr(self.config, "snapshot_max_age", 30.0)
            if self.transport == "snapshot"
            else self.config.max_tick_age
        )

    def running(self):
        return self.task is not None and not self.task.done()

    async def start(self, bot, chat_id, minutes, timeframes):
        if (
            minutes not in (0, 30, 60, 120, 240, 480)
            or not timeframes
            or any(t not in TIMEFRAMES for t in timeframes)
        ):
            raise ValueError("Invalid session selection")
        async with self._lock:
            if self.running():
                return False
            self.chat_id = chat_id
            self.timeframes = tuple(timeframes)
            self.ends_at = self.clock() + minutes * 60 if minutes else None
            self.last_tick = None
            self._quotes.clear()
            self.last_error = None
            self.transport = "live"
            self.poll_requests_used = 0
            self.available = 0
            self.phase = "подключение"
            self.task = asyncio.create_task(self._timed_run(bot))
            return True

    async def stop(self):
        async with self._lock:
            if not self.running():
                return False
            self.task.cancel()
            try:
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
            finally:
                # A task cancelled before its first instruction has no finally block.
                self.phase = "остановлен"
                self.monitored = 0
                self.last_tick = None
                self._quotes.clear()
                await self.provider.close()
                if self.db:
                    await db_call(self.db.recover)
            return True

    def status(self):
        fresh = (
            self.last_tick is not None
            and 0 <= self.clock() - self.last_tick <= self._price_max_age()
        )
        phase = self.phase
        if phase == "поток активен" and not fresh:
            phase = "нет свежих котировок"
        if self.transport == "snapshot" and not fresh and self.running():
            phase = "ожидание свежего снимка свечей"
        monitored = sum(
            0 <= self.clock() - t <= self._price_max_age()
            for t in self._quotes.values()
        )
        end = (
            datetime.fromtimestamp(self.ends_at, MOSCOW).strftime("%H:%M МСК")
            if self.ends_at
            else "до остановки"
        )
        age = (
            f"{self.clock() - self.last_tick:.1f} с"
            if self.last_tick is not None
            else "нет данных"
        )
        return (
            f"OTC: {phase}\nРежим: {'тестовый — выплата/открытие могут быть неизвестны' if self.test_mode else 'строгий — открытие и выплата ≥90% подтверждены'}"
            f"\nДанные: {'снимки свечей без live-потока' if self.transport == 'snapshot' else 'поток'}\nЭкспирации: {', '.join(f'M{x // 60}' for x in self.timeframes)}"
            f"\nДо: {end}\nОтслеживается: {monitored} из {self.available}.\n{'С закрытия свечи' if self.transport == 'snapshot' else 'Возраст котировки'}: {age}"
            "\nЭКСПЕРИМЕНТАЛЬНЫЙ РЕЖИМ. Совпадение с платформой не подтверждено."
            + (
                f"\nТариф: {self.plan.plan_name}; запросы: {self.plan.remaining}/{self.plan.quota} за {self.plan.per}; потоки: {self.plan.streams_open}/{self.plan.streams_limit}; активов/поток: {self.plan.instruments_per_stream if self.plan.instruments_per_stream is not None else 'без лимита'}. Лимиты получены при последней проверке."
                if self.plan
                else ""
            )
            + (
                f"\nСнимки: 1 инструмент, интервал {getattr(self.config, 'poll_seconds', 300)} с; запросов за сеанс {self.poll_requests_used}/{getattr(self.config, 'poll_request_budget', 12)}."
                if self.transport == "snapshot"
                else ""
            )
            + (f"\n{self.last_error}" if self.last_error else "")
        )

    async def _notify(self, bot, text):
        try:
            await bot.send_message(self.chat_id, text)
        except Exception:
            logger.warning("Telegram notification failed")

    async def _expiry_watchdog(self):
        while True:
            await asyncio.sleep(1)
            if self.db:
                for row in await db_call(self.db.pending):
                    if (
                        self.clock()
                        > row["expires_at"] + self.config.settlement_tolerance
                    ):
                        await db_call(self.db.finish, row["id"], "UNKNOWN")

    async def _timed_run(self, bot):
        watchdog = asyncio.create_task(self._expiry_watchdog())
        try:
            if self.ends_at is None:
                await self._run(bot)
            else:
                async with asyncio.timeout(max(0, self.ends_at - self.clock())):
                    await self._run(bot)
        except TimeoutError:
            await self._notify(bot, "Время OTC-сеанса закончилось.")
        except AccessUnavailable as exc:
            # Exception text originates solely in our sanitized adapter.
            self.last_error = str(exc)
            await self._notify(bot, self.last_error)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = (
                f"OTC остановлен: {type(exc).__name__}. Подробности /otc_diagnostics."
            )
            logger.warning("OTC stopped: %s", type(exc).__name__)
            await self._notify(bot, self.last_error)
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
            self.phase = "остановлен"
            self.monitored = 0
            self._quotes.clear()
            self.last_tick = None
            try:
                if self.db:
                    await db_call(self.db.recover)
            finally:
                await self.provider.close()

    async def _run(self, bot):
        plan = await self.provider.usage()
        self.plan = plan
        if self.test_mode and plan.streams_limit <= 0:
            await self._poll(bot, plan)
            return
        if plan.streams_limit <= plan.streams_open:
            raise AccessUnavailable(
                "Нет доступа к потоковым котировкам/выплатам или нет свободного потока."
            )
        assets = self._eligible(
            await self.provider.symbols(), self.clock(), self.config.payout_max_age
        )
        self.available = len(assets)
        if not assets:
            raise AccessUnavailable(
                "Нет открытых OTC с подтверждённой свежей выплатой ≥90%. "
                "OTCharts 0.7.0 не передаёт timestamp выплаты; open для Pocket Option может быть неизвестен. Сигналы заблокированы."
            )
        cap = self.config.max_instruments
        if plan.instruments_per_stream is not None:
            cap = min(cap, max(0, int(plan.instruments_per_stream)))
        if cap <= 0:
            raise AccessUnavailable("Нет доступных инструментов на поток.")
        chosen = assets[:cap]
        # One catalogue, one stream, history per instrument; leave a reserve.
        if plan.remaining < len(chosen) + 3:
            raise AccessUnavailable(
                "Недостаточно запросов OTCharts для истории и потока."
            )
        history = {}
        for asset in chosen:
            history[asset.symbol] = await self.provider.candles(
                asset.symbol, self.clock()
            )
        await self._scan(bot, history, {a.symbol: a for a in chosen})

    async def _wait_poll(self):
        interval = max(60, getattr(self.config, "poll_seconds", 300))
        # Align on a future candle boundary +2s, not request completion drift.
        wake = (int(self.clock()) // interval + 1) * interval + 2
        await asyncio.sleep(max(1, wake - self.clock()))

    async def _poll(self, bot, plan):
        """Small bounded REST sample for plans without streaming; never emulate ticks."""
        self.transport = "snapshot"
        self.phase = "получение списка для теста"
        budget = max(2, getattr(self.config, "poll_request_budget", 12))
        if plan.remaining < 3:
            raise AccessUnavailable(
                "Недостаточно запросов для каталога и снимка; оставлен резерв."
            )
        self.poll_requests_used += 1
        assets = self._eligible(
            await self.provider.symbols(), self.clock(), self.config.payout_max_age
        )
        self.available = len(assets)
        self.plan = replace(plan, remaining=max(0, plan.remaining - 1))
        if not assets:
            raise AccessUnavailable(
                "Нет доступных OTC для теста. Подтверждённо закрытые инструменты пропускаются."
            )
        # One pair protects a small nonrenewing free allowance.
        asset = assets[0]
        await self._notify(
            bot,
            f"ТЕСТОВЫЙ РЕЖИМ: {asset.symbol}, 1 из {len(assets)} OTC. "
            f"Live-потока на тарифе нет. Анализ снимков свечей раз в {getattr(self.config, 'poll_seconds', 300)} с; "
            f"не более {budget} API-запросов за сеанс. Выплата и открытие могут быть неизвестны. "
            "Сигналы появятся только при свежих свечах и выполнении условий стратегии.",
        )
        seen = None
        while self.poll_requests_used < budget:
            self.plan = await self.provider.usage()  # Documented as a free quota check.
            if self.plan.remaining <= 1:
                raise AccessUnavailable(
                    "Лимит OTCharts почти исчерпан; тест остановлен с резервом одного запроса."
                )
            self.phase = "анализ снимка свечей"
            self.poll_requests_used += 1
            self.plan = replace(self.plan, remaining=max(0, self.plan.remaining - 1))
            try:
                bars = await self.provider.candles(asset.symbol, self.clock())
                # Revalidate complete UTC bars even for injected providers.
                bars = normalize_candles([vars(b) for b in bars], int(self.clock()))
                if not bars:
                    await self._event("snapshot_no_history", asset.symbol)
                else:
                    closed_at = bars[-1].time + 60
                    self.last_tick = closed_at
                    self._quotes[asset.symbol] = closed_at
                    self.monitored = int(
                        0 <= self.clock() - closed_at <= self._price_max_age()
                    )
                    if not 0 <= self.clock() - closed_at <= self._price_max_age():
                        await self._event("stale_snapshot", asset.symbol)
                    elif seen != closed_at:
                        seen = closed_at
                        tick = Tick(asset.symbol, closed_at, bars[-1].close)
                        for seconds in self.timeframes:
                            if closed_at % seconds:
                                continue
                            result = candidate(bars, seconds, closed_at)
                            if result:
                                await self._send(
                                    bot, tick, asset, seconds, result, closed_at
                                )
                            else:
                                await self._event(
                                    f"no_candidate_M{seconds // 60}", asset.symbol
                                )
            except (AccessUnavailable, asyncio.CancelledError):
                raise
            except Exception as exc:
                # Failures can still spend quota; never spin or retry outside the budget.
                await self._event(f"snapshot_error_{type(exc).__name__}", asset.symbol)
            if self.poll_requests_used >= budget:
                break
            self.phase = "ожидание следующего снимка"
            await self._wait_poll()
        await self._notify(
            bot,
            "Тест остановлен: исчерпан заданный бюджет запросов сеанса. Остаток тарифа — /otc_status.",
        )

    async def _event(self, reason, symbol=None):
        # Bound repeated rejection writes without hiding the latest cause.
        key = (symbol, reason)
        now = self.clock()
        if self.db and now - self._last_events.get(key, float("-inf")) >= 60:
            self._last_events[key] = now
            await db_call(self.db.event, reason, symbol, now)

    async def _scan(self, bot, history, chosen):
        attempt = 0
        builder = MinuteBuilder(self.config.max_tick_age, self.config.max_tick_gap)
        active = dict(chosen)
        refreshed = self.clock()
        while True:
            try:
                self.phase = "ожидание свежего тика"
                self.monitored = 0
                async with contextlib.aclosing(
                    self.provider.ticks(list(chosen))
                ) as stream:
                    async for tick in stream:
                        now = self.clock()
                        if tick.symbol not in history:
                            continue
                        bar = builder.push(tick, now)
                        if builder.rejection in (
                            "invalid_or_stale_tick",
                            "duplicate_or_unordered_tick",
                        ):
                            await self._event(builder.rejection, tick.symbol)
                            continue
                        self.last_tick = tick.time
                        self._quotes[tick.symbol] = tick.time
                        self.monitored = sum(
                            0 <= now - t <= self.config.max_tick_age
                            for t in builder.last.values()
                        )
                        self.phase = "поток активен"
                        attempt = 0
                        if self.db:
                            await self._results(tick)
                        if now - refreshed >= 30:
                            current = await self.provider.usage()
                            self.plan = current
                            if current.remaining <= 1:
                                raise AccessUnavailable(
                                    "Лимит запросов исчерпан; поток остановлен."
                                )
                            active = {
                                a.symbol: a
                                for a in self._eligible(
                                    await self.provider.symbols(),
                                    self.clock(),
                                    self.config.payout_max_age,
                                )
                                if a.symbol in chosen
                            }
                            refreshed = self.clock()
                        if not bar:
                            if builder.rejection:
                                await self._event(builder.rejection, tick.symbol)
                            continue
                        values = history[tick.symbol]
                        if not values or values[-1].time < bar.time:
                            values.append(bar)
                            del values[:-1200]
                        asset = active.get(tick.symbol)
                        now = self.clock()
                        if not asset or not self._eligible(
                            [asset], now, self.config.payout_max_age
                        ):
                            await self._event("unknown_or_stale_payout", tick.symbol)
                            continue
                        closed_at = bar.time + 60
                        if (
                            tick.time - closed_at > 2
                            or now - tick.time > self.config.max_tick_age
                        ):
                            await self._event("late_signal", tick.symbol)
                            continue
                        for seconds in self.timeframes:
                            if closed_at % seconds:
                                continue
                            result = candidate(values, seconds, closed_at)
                            if result:
                                await self._send(
                                    bot, tick, asset, seconds, result, closed_at
                                )
                            else:
                                await self._event(
                                    f"no_candidate_M{seconds // 60}", tick.symbol
                                )
                raise ConnectionError("stream ended")
            except asyncio.CancelledError:
                raise
            except AccessUnavailable:
                raise
            except Exception as exc:
                self.phase = "Переподключение"
                self.monitored = 0
                self.last_tick = None
                builder.clear()
                self._quotes.clear()
                active = {}
                # Broken continuity invalidates outstanding observations.
                if self.db:
                    await db_call(self.db.recover)
                await self._event("stream_disconnected")
                delay = reconnect_delay(attempt, getattr(exc, "retry_after", 0))
                attempt += 1
                await asyncio.sleep(delay)
                plan = await self.provider.usage()
                if plan.remaining < len(chosen) + 2:
                    raise AccessUnavailable(
                        "Недостаточно запросов для восстановления истории."
                    )
                active = {
                    a.symbol: a
                    for a in self._eligible(
                        await self.provider.symbols(),
                        self.clock(),
                        self.config.payout_max_age,
                    )
                    if a.symbol in chosen
                }
                refreshed = self.clock()
                for symbol in chosen:
                    history[symbol] = await self.provider.candles(symbol, self.clock())

    async def _send(self, bot, tick, asset, seconds, result, closed_at):
        now = self.clock()
        if not 0 <= now - tick.time <= self._price_max_age() or not self._eligible(
            [asset], now, self.config.payout_max_age
        ):
            await self._event("late_signal_or_stale_payout", tick.symbol)
            return
        direction, reason = result
        payout = (
            asset.payout
            if isinstance(asset.payout, (int, float))
            and not isinstance(asset.payout, bool)
            and math.isfinite(asset.payout)
            and 0 <= asset.payout <= 100
            else None
        )
        payout_confirmed = (
            payout is not None
            and isinstance(asset.payout_at, (int, float))
            and math.isfinite(asset.payout_at)
            and 0 <= now - asset.payout_at <= self.config.payout_max_age
        )
        open_confirmed = asset.open is True and self.transport != "snapshot"
        signal_at = now if self.transport == "snapshot" else tick.time
        strategy = "ema-rsi-atr-v1" + (
            "-test-" + self.transport if self.test_mode else ""
        )
        quality = {
            "closed_at": closed_at,
            "quote_at": tick.time,
            "test_mode": self.test_mode,
            "transport": self.transport,
            "payout_confirmed": payout_confirmed,
            "open_confirmed": open_confirmed,
            "catalogue_open": asset.open,
            "quote_age": self.clock() - tick.time,
            "platform_match": False,
            "complete_candles": True,
        }
        identifier = await db_call(
            self.db.record,
            symbol=tick.symbol,
            direction=direction,
            timeframe=seconds,
            signal_at=signal_at,
            price=tick.price,
            payout=payout,
            payout_at=asset.payout_at,
            reason=reason,
            quality=quality,
            source=self.provider.source,
            synchronized=False,
            cooldown=self.config.cooldown_seconds,
            strategy=strategy,
        )
        if identifier is None:
            await self._event("cooldown", tick.symbol)
            return
        when = datetime.fromtimestamp(signal_at, MOSCOW)
        expires = datetime.fromtimestamp(signal_at + seconds, MOSCOW)
        payout_text = (
            "неизвестна"
            if payout is None
            else f"{payout:g}%"
            + (
                " (подтверждена)"
                if payout_confirmed
                else " (актуальность не подтверждена)"
            )
        )
        open_text = "подтверждено" if open_confirmed else "не подтверждено"
        price_label = "Цена закрытия свечи" if self.transport == "snapshot" else "Цена"
        test_label = "ТЕСТОВЫЙ РЕЖИМ · " if self.test_mode else ""
        text = (
            f"{test_label}ЭКСПЕРИМЕНТАЛЬНЫЙ РЕЖИМ\n{tick.symbol} · {'CALL / UP' if direction == 'UP' else 'PUT / DOWN'}"
            f"\nЭкспирация M{seconds // 60} · {expires:%H:%M:%S} МСК\nСигнал {when:%H:%M:%S} МСК"
            f"\n{price_label} {tick.price:g} · выплата: {payout_text}\nОткрытие инструмента: {open_text}"
            f"\nИсточник {self.provider.source} · {'с закрытия свечи' if self.transport == 'snapshot' else 'возраст цены'} {quality['quote_age']:.1f} с"
            + (
                "\nСнимок свечей, live-потока нет. Текущая цена входа неизвестна."
                if self.transport == "snapshot"
                else ""
            )
            + f"\nПричина: {reason}\nID: {identifier}\nСовпадение с платформой не подтверждено. Виртуальный исход UNKNOWN."
        )
        try:
            await bot.send_message(self.chat_id, text)
        except Exception:
            await db_call(self.db.delivered, identifier, success=False)
            await db_call(self.db.finish, identifier, "UNKNOWN")
            await self._event("delivery_failed", tick.symbol)
            return
        await db_call(self.db.delivered, identifier, self.clock())
        # An intended entry is not proof of a real trade or actual entry.

    async def _results(self, tick):
        for row in await db_call(self.db.pending, tick.symbol):
            outcome = settle(row, tick, self.config.settlement_tolerance)
            if outcome:
                await db_call(self.db.finish, row["id"], outcome, tick.price)
        # Missing expiry ticks must not leave signals PENDING indefinitely.
        for row in await db_call(self.db.pending):
            if self.clock() > row["expires_at"] + self.config.settlement_tolerance:
                await db_call(self.db.finish, row["id"], "UNKNOWN")
