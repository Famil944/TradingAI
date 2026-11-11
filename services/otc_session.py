"""Single-owner research sessions. No trading or broker credentials."""

import asyncio
import contextlib
import logging
import math
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from services.otc_conditional import (
    context_setup as conditional_setup,
)
from services.otc_conditional import (
    make_plan,
)
from services.otc_conditional import (
    message as conditional_message,
)
from services.otc_data import AccessUnavailable, OTChartsProvider, RetryableFeedError
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
        self.last_signal_at = None
        self.session_signal_count = 0
        self.session_plan_count = 0
        self._health_counts = Counter()
        self._repair_last = {}
        self._repair_count = 0

    @property
    def conditional_mode(self):
        return getattr(self.config, "signal_mode", "immediate") == "conditional"

    @property
    def test_mode(self):
        return bool(getattr(self.config, "test_mode", False))

    def _eligible(self, instruments, now, max_age):
        if getattr(self.config, "require_paid_metadata", False):
            instruments = [
                a
                for a in instruments
                if a.open is True
                and a.payout is not None
                and math.isfinite(a.payout)
                and 90 <= a.payout <= 100
                and getattr(a, "observed_at", None) is not None
                and 0 <= now - a.observed_at <= max_age
            ]
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
            self.transport = "pending"
            self.poll_requests_used = 0
            self.last_signal_at = None
            self.session_signal_count = 0
            self.session_plan_count = 0
            self._health_counts.clear()
            self._repair_last.clear()
            self._repair_count = 0
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
                    await db_call(self.db.close_plans, self.clock(), cancel=True)
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
            f"OTC: {phase}\nСигналы: {'условные планы: вход по времени закрытия свечи' if self.conditional_mode else 'обычный режим'}; планов за сеанс {self.session_plan_count}"
            f"\nРежим: {'тестовый · текущая выплата ≥90% при включённом отборе' if self.test_mode else 'строгий — открытие и выплата ≥90% подтверждены'}"
            f"\nДанные: { {'snapshot': 'снимки свечей без live-потока', 'live': 'поток', 'pending': 'ещё не определены (проверка доступа)'}.get(self.transport, 'нет данных') }\nЭкспирации: {', '.join(f'M{x // 60}' for x in self.timeframes)}"
            f"\nСигналов за сеанс: {self.session_signal_count}; пауза по паре/экспирации {self.config.cooldown_seconds} с."
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
            async with asyncio.timeout(10):
                await bot.send_message(self.chat_id, text)
        except Exception as exc:
            logger.warning("Telegram notification failed: %s", type(exc).__name__)

    async def _health_report(self, bot):
        counts = self._health_counts.copy()
        self._health_counts.clear()
        status = self.status()
        logger.info(
            "OTC health: %s; reasons=%s", status.replace("\n", " | "), dict(counts)
        )
        interval = getattr(self.config, "health_report_seconds", 120)
        if (
            self.last_signal_at is not None
            and 0 <= self.clock() - self.last_signal_at < interval
        ):
            return  # The signal itself is already an update to the owner.
        labels = {
            "invalid_or_stale_tick": "старые или недопустимые тики",
            "duplicate_or_unordered_tick": "повторные/неупорядоченные тики",
            "incomplete_candle": "неполные свечи",
            "stream_no_fresh_ticks": "поток без свежих цен",
            "stream_disconnected": "разрывы потока",
            "cooldown": "пауза между сигналами",
            "late_signal": "запоздалый расчёт",
            "unknown_or_stale_payout": "нет подтверждённой выплаты",
            "stale_snapshot": "старый снимок свечей",
            "history_repaired": "непрерывная история восстановлена",
            "history_repair_failed": "не удалось восстановить историю",
            "catalogue_refresh_failed": "не удалось обновить каталог, планы приостановлены",
            "conditional_no_setup": "контекст для условного плана не сформирован",
            "conditional_pending_or_cooldown": "уже есть план или действует пауза",
            "conditional_too_late_or_session_ending": "недостаточно запаса времени до входа/конца сеанса",
            "conditional_stale_data": "устаревшие данные для плана",
            "conditional_delivery_failed": "ошибка доставки плана",
            "conditional_delivery_late": "поздняя доставка плана — пропуск",
        }
        lines = []
        for reason, count in counts.most_common(4):
            label = (
                "условия стратегии не выполнены ("
                + reason.removeprefix("no_candidate_")
                + ")"
                if reason.startswith("no_candidate_")
                else labels.get(reason, reason)
            )
            lines.append(f"{label}: {count}")
        explanation = (
            "; ".join(lines)
            if lines
            else "ожидание данных, истории или закрытия полной свечи"
        )
        await self._notify(
            bot,
            f"КОНТРОЛЬ РАБОТЫ — это не торговый сигнал.\n"
            f"За последние {interval:g} с нового сигнала нет. Причины: {explanation}.\n"
            + status,
        )

    async def _health_watchdog(self, bot):
        interval = getattr(self.config, "health_report_seconds", 120)
        if interval <= 0:
            return
        while True:
            await asyncio.sleep(interval)
            await self._health_report(bot)

    async def _expiry_watchdog(self):
        while True:
            await asyncio.sleep(1)
            if self.db:
                if self.conditional_mode:
                    await db_call(self.db.close_plans, self.clock())
                for row in await db_call(self.db.pending):
                    if (
                        self.clock()
                        > row["expires_at"] + self.config.settlement_tolerance
                    ):
                        await db_call(self.db.finish, row["id"], "UNKNOWN")

    async def _timed_run(self, bot):
        watchdog = asyncio.create_task(self._expiry_watchdog())
        health_watchdog = asyncio.create_task(self._health_watchdog(bot))
        deadline = None
        try:
            if self.ends_at is None:
                await self._run(bot)
            else:
                async with asyncio.timeout(
                    max(0, self.ends_at - self.clock())
                ) as deadline:
                    await self._run(bot)
        except TimeoutError:
            if deadline is not None and deadline.expired():
                await self._notify(bot, "Время OTC-сеанса закончилось.")
            else:
                self.last_error = "OTCharts не ответил вовремя. Это сетевой тайм-аут, а не окончание времени сеанса. Попробуйте запустить снова."
                await self._notify(bot, self.last_error)
        except RetryableFeedError:
            logger.warning("OTC startup stopped after transient network failure")
            self.last_error = "Не удалось подключиться к OTCharts после повторных попыток. Данные недоступны; сеанс остановлен. Попробуйте запустить снова."
            await self._notify(bot, self.last_error)
        except AccessUnavailable as exc:
            # Exception text originates solely in our sanitized adapter.
            self.last_error = str(exc)
            logger.warning("OTC access unavailable: %s", self.last_error)
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
            health_watchdog.cancel()
            await asyncio.gather(watchdog, health_watchdog, return_exceptions=True)
            self.phase = "остановлен"
            self.monitored = 0
            self._quotes.clear()
            self.last_tick = None
            try:
                if self.db:
                    await db_call(self.db.recover)
                    await db_call(self.db.close_plans, self.clock(), cancel=True)
            finally:
                await self.provider.close()

    async def _usage_with_retry(self):
        # Usage checks are free under the verified contract; retries spend no data quota.
        for attempt in range(3):
            try:
                return await self.provider.usage()
            except (RetryableFeedError, TimeoutError, ConnectionError) as exc:
                if attempt == 2:
                    raise RetryableFeedError() from None
                if self.transport == "pending":
                    self.phase = f"повтор подключения к OTCharts ({attempt + 2}/3)"
                await self._event("usage_connection_retry")
                await asyncio.sleep(
                    reconnect_delay(attempt, getattr(exc, "retry_after", 0))
                )

    async def _wait_stream_release(self):
        await asyncio.sleep(5)

    async def _catalogue_with_retry(self):
        for attempt in range(3):
            try:
                return await self.provider.symbols()
            except (RetryableFeedError, TimeoutError):
                if attempt == 2:
                    raise
                logger.warning("OTCharts catalogue startup retry %d/3", attempt+2)
                await asyncio.sleep(1)

    async def _run(self, bot):
        plan = await self._usage_with_retry()
        self.plan = plan
        logger.info(
            "OTCharts access: plan=%s; requests=%s; streams=%s/%s",
            plan.plan_name,
            plan.remaining,
            plan.streams_open,
            plan.streams_limit,
        )
        if self.test_mode and plan.streams_limit <= 0:
            await self._poll(bot, plan)
            return
        self.transport = "live"
        if plan.streams_limit > 0 and plan.streams_open >= plan.streams_limit:
            # A just-closed connection may take a few seconds to release its slot.
            for _ in range(3):
                self.phase = "ожидание освобождения потока OTCharts"
                await self._wait_stream_release()
                plan = await self._usage_with_retry()
                self.plan = plan
                if plan.streams_open < plan.streams_limit:
                    break
        if plan.streams_limit <= plan.streams_open:
            raise AccessUnavailable(
                "Нет доступа к потоковым котировкам/выплатам или нет свободного потока."
            )
        assets = self._eligible(
            await self._catalogue_with_retry(), self.clock(), self.config.payout_max_age
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
        logger.info("OTC selected: %d of %d instruments", len(chosen), len(assets))
        # One catalogue, one stream, history per instrument; leave a reserve.
        if plan.remaining < len(chosen) + 3:
            raise AccessUnavailable(
                "Недостаточно запросов OTCharts для истории и потока."
            )
        history = {}
        for index, asset in enumerate(chosen, 1):
            self.phase = f"загрузка истории {index}/{len(chosen)}"
            history[asset.symbol] = await self.provider.candles(
                asset.symbol, self.clock()
            )
            logger.info(
                "OTC history %s: %d closed candles",
                asset.symbol,
                len(history[asset.symbol]),
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
            self.plan = (
                await self._usage_with_retry()
            )  # Documented as a free quota check.
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
                        if self.conditional_mode:
                            for seconds in self.timeframes:
                                await self._consider_plan(
                                    bot, bars, tick, asset, closed_at, seconds
                                )
                        else:
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
        self._health_counts[reason] += 1
        key = (symbol, reason)
        now = self.clock()
        if self.db and now - self._last_events.get(key, float("-inf")) >= 60:
            self._last_events[key] = now
            await db_call(self.db.event, reason, symbol, now)

    async def _repair_history(self, symbol, history):
        """Backfill fully closed provider bars after an incomplete live minute."""
        try:
            plan = await self._usage_with_retry()
            self.plan = plan
            if plan.remaining <= 1:
                await self._event("history_repair_quota", symbol)
                return
            fresh = await self.provider.candles(symbol, self.clock())
            fresh = normalize_candles([vars(b) for b in fresh], int(self.clock()))
            if not fresh:
                await self._event("history_repair_empty", symbol)
                return
            # Provider closed bars are canonical; preserve newer live bars.
            latest = fresh[-1].time
            tail = [b for b in history.get(symbol, []) if b.time > latest]
            merged = {b.time: b for b in fresh + tail}
            history[symbol] = [merged[t] for t in sorted(merged)][-1200:]
            logger.info(
                "OTC history continuity restored: %s, %d bars",
                symbol,
                len(history[symbol]),
            )
            await self._event("history_repaired", symbol)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "OTC history repair failed: %s (%s)", symbol, type(exc).__name__
            )
            await self._event("history_repair_failed", symbol)

    def _queue_history_repair(self, symbol, history, tasks):
        now = self.clock()
        if (
            self._repair_count >= 50
            or now - self._repair_last.get(symbol, float("-inf")) < 60
        ):
            return
        existing = tasks.get(symbol)
        if existing is not None and not existing.done():
            return
        self._repair_last[symbol] = now
        self._repair_count += 1
        tasks[symbol] = asyncio.create_task(self._repair_history(symbol, history))

    async def _refresh_stream_assets(self, active, chosen):
        while True:
            await asyncio.sleep(getattr(self.config, "catalogue_refresh_seconds", 30))
            try:
                current = await self._usage_with_retry()
                self.plan = current
                if current.remaining <= 1:
                    raise AccessUnavailable(
                        "Лимит запросов исчерпан; поток остановлен."
                    )
                catalogue = self._eligible(
                    await self.provider.symbols(),
                    self.clock(),
                    self.config.payout_max_age,
                )
                updated = {a.symbol: a for a in catalogue if a.symbol in chosen}
                active.clear()
                active.update(updated)
                self.available = len(catalogue)
            except AccessUnavailable:
                active.clear()
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                active.clear()
                logger.warning("OTC catalogue refresh failed: %s", type(exc).__name__)
                await self._event("catalogue_refresh_failed")

    async def _scan(self, bot, history, chosen):
        attempt = 0
        builder = MinuteBuilder(self.config.max_tick_age, self.config.max_tick_gap)
        active = dict(chosen)
        while True:
            try:
                self.phase = "ожидание свежего тика"
                logger.info("OTC waiting for fresh ticks: %d instruments", len(chosen))
                self.monitored = 0
                loop = asyncio.get_running_loop()
                stale_timeout = getattr(self.config, "stream_stale_timeout", 15.0)
                fresh_deadline = loop.time() + stale_timeout
                healthy_since = None
                refresh_task = asyncio.create_task(
                    self._refresh_stream_assets(active, chosen)
                )
                repair_tasks = {}
                try:
                    async with contextlib.aclosing(
                        self.provider.ticks(list(chosen))
                    ) as stream:
                        while True:
                            if refresh_task.done():
                                refresh_task.result()
                            # SSE keepalives and stale/duplicate events do not prove fresh data.
                            if loop.time() >= fresh_deadline:
                                raise TimeoutError("fresh market data deadline")
                            try:
                                async with asyncio.timeout_at(fresh_deadline):
                                    tick = await anext(stream)
                            except StopAsyncIteration:
                                raise ConnectionError("stream ended") from None
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
                            fresh_deadline = loop.time() + stale_timeout
                            if healthy_since is None:
                                healthy_since = loop.time()
                                logger.info("OTC fresh stream confirmed")
                            if loop.time() - healthy_since >= 30:
                                attempt = 0
                            self.phase = "поток активен"
                            if self.db:
                                await self._results(tick)
                            if not bar:
                                if builder.rejection:
                                    await self._event(builder.rejection, tick.symbol)
                                    if builder.rejection == "incomplete_candle":
                                        self._queue_history_repair(
                                            tick.symbol, history, repair_tasks
                                        )
                                continue
                            if hasattr(self.provider, "record_closed"):
                                await self.provider.record_closed(
                                    tick.symbol, bar, self.clock()
                                )
                            values = history[tick.symbol]
                            if values and values[-1].time + 60 < bar.time:
                                self._queue_history_repair(
                                    tick.symbol, history, repair_tasks
                                )
                            if not values or values[-1].time < bar.time:
                                values.append(bar)
                                del values[:-1200]
                            asset = active.get(tick.symbol)
                            now = self.clock()
                            if tick.symbol in getattr(
                                self.provider, "closed_symbols", set()
                            ):
                                await self._event("instrument_closed", tick.symbol)
                                continue
                            if not asset or not self._eligible(
                                [asset], now, self.config.payout_max_age
                            ):
                                await self._event(
                                    "unknown_or_stale_payout", tick.symbol
                                )
                                continue
                            closed_at = bar.time + 60
                            if self.conditional_mode:
                                for seconds in self.timeframes:
                                    await self._consider_plan(
                                        bot, values, tick, asset, closed_at, seconds
                                    )
                                continue
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
                finally:
                    refresh_task.cancel()
                    for task in repair_tasks.values():
                        task.cancel()
                    await asyncio.gather(
                        refresh_task, *repair_tasks.values(), return_exceptions=True
                    )
            except asyncio.CancelledError:
                raise
            except AccessUnavailable:
                raise
            except Exception as exc:
                if refresh_task.done() and not refresh_task.cancelled():
                    refresh_error = refresh_task.exception()
                    if isinstance(refresh_error, AccessUnavailable):
                        raise refresh_error
                self.phase = "Переподключение"
                self.monitored = 0
                self.last_tick = None
                builder.clear()
                self._quotes.clear()
                active = {}
                # Broken continuity invalidates outstanding observations.
                if self.db:
                    await db_call(self.db.recover)
                reason = (
                    "stream_no_fresh_ticks"
                    if isinstance(exc, TimeoutError)
                    else "stream_disconnected"
                )
                logger.warning("OTC reconnect: %s (%s)", reason, type(exc).__name__)
                await self._event(reason)
                delay = reconnect_delay(attempt, getattr(exc, "retry_after", 0))
                attempt += 1
                await asyncio.sleep(delay)
                plan = await self._usage_with_retry()
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
                for symbol in chosen:
                    history[symbol] = await self.provider.candles(symbol, self.clock())

    async def _consider_plan(self, bot, history, tick, asset, closed_at, seconds=60):
        now = self.clock()
        if not 0 <= now - tick.time <= self._price_max_age() or not self._eligible(
            [asset], now, self.config.payout_max_age
        ):
            await self._event("conditional_stale_data", tick.symbol)
            return
        context = (
            conditional_setup(history, closed_at)
            if seconds == 60
            else conditional_setup(history, closed_at, seconds)
        )
        if context is None:
            await self._event("conditional_no_setup", tick.symbol)
            return
        plan = make_plan(
            tick.symbol, closed_at, context, now, self.ends_at, seconds=seconds
        )
        if plan is None:
            await self._event("conditional_too_late_or_session_ending", tick.symbol)
            return
        payout = (
            asset.payout
            if isinstance(asset.payout, (int, float))
            and not isinstance(asset.payout, bool)
            and math.isfinite(asset.payout)
            and 0 <= asset.payout <= 100
            else None
        )
        if context.evidence is not None:
            context.evidence["market"] = {
                "source": "otcharts_catalogue",
                "name": getattr(asset, "name", None),
                "open": asset.open,
                "payout": payout,
                "observed_at": getattr(asset, "observed_at", None),
                "payout_at": asset.payout_at,
                "entry_payout_guaranteed": False,
            }
        identifier = await db_call(
            self.db.prepare_plan,
            plan,
            now,
            payout,
            asset.payout_at,
            cooldown=self.config.cooldown_seconds,
            strategy=context.rule
            + (
                ":entry-recheck-v1"
                if getattr(self.config, "entry_quality_recheck", False)
                else ""
            ),
        )
        if identifier is None:
            await self._event("conditional_pending_or_cooldown", tick.symbol)
            return
        if self.clock() > plan.delivery_deadline:
            await db_call(self.db.plan_delivered, identifier, self.clock(), False)
            return
        try:
            async with asyncio.timeout(
                min(10, max(0.1, plan.delivery_deadline - self.clock()))
            ):
                await bot.send_message(
                    self.chat_id,
                    conditional_message(
                        plan, identifier, payout, name=getattr(asset, "name", None)
                    ),
                )
        except Exception:
            await db_call(self.db.plan_delivered, identifier, self.clock(), False)
            await self._event("conditional_delivery_failed", tick.symbol)
            return
        sent = self.clock()
        await db_call(self.db.plan_delivered, identifier, sent, True)
        if sent <= plan.delivery_deadline:
            self.session_plan_count += 1
            self.last_signal_at = sent
        else:
            await self._event("conditional_delivery_late", tick.symbol)

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
        self.last_signal_at = self.clock()
        self.session_signal_count += 1
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
