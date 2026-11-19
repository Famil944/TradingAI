"""Single-owner research sessions. No trading or broker credentials."""

import asyncio
import contextlib
import logging
import math
import os
import sqlite3
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
from services.otc_data import (
    AccessUnavailable,
    OTChartsProvider,
    RetryableFeedError,
    StreamSlotBusy,
)
from services.otc_market import TIMEFRAMES, MinuteBuilder, Tick, eligible, settle
from services.otc_pair_rotation import (
    choose_pair_swap,
    journal_activity,
    usable_history,
)
from services.otc_strategy import candidate, normalize_candles
from services.otc_stream import reconnect_delay
from services.otc_history_repair import reconcile_closed_history, contiguous_tail_minutes

logger = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")
# Restore missing historical candles by default; users can opt out in .env.
HISTORY_RECOVERY_ENABLED = os.getenv("OTC_HISTORY_RECOVERY_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


def sqlite_busy(exc):
    """Retry only transient SQLite contention, never missing schema/corrupt DB."""
    return isinstance(exc, sqlite3.OperationalError) and any(
        marker in str(exc).lower() for marker in ("locked", "busy")
    )


async def db_call(function, *args, **kwargs):
    """Drain SQLite work on cancellation before shutdown or another session."""
    for attempt in range(3):
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        except sqlite3.OperationalError as exc:
            if not sqlite_busy(exc) or attempt == 2:
                raise
            logger.warning("OTC SQLite is temporarily busy; retry %d/3", attempt + 2)
            await asyncio.sleep((attempt + 1) * 0.5)


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
        self.closed_history = {}
        self.plan = None
        self.transport = "live"
        self.poll_requests_used = 0
        self.last_signal_at = None
        self.session_signal_count = 0
        self.session_plan_count = 0
        self._health_counts = Counter()
        self._repair_last = {}
        self._repair_count = 0
        self._repair_window = self.clock()
        self._rotation_selected_at = {}
        self._rotation_last_removed = {}
        self._rotation_last_candle = {}
        self._rotation_pending = None
        self._rotation_next_at = 0
        self.active_symbols = ()
        self.rotations = 0
        self._tick_diagnostics = {}
        self._tick_diagnostics_since = self.clock()

    def _record_tick_diagnostic(self, symbol, now, *, accepted=False, rejection=None):
        """Observe feed freshness without modifying market data or entry gates."""
        stats = self._tick_diagnostics.setdefault(
            symbol, {"accepted": 0, "rejected": Counter(), "last_received": None,
                     "last_accepted": None, "last_quote": None}
        )
        stats["last_received"] = now
        if accepted:
            stats["accepted"] += 1
            stats["last_accepted"] = now
            stats["last_quote"] = self._quotes.get(symbol)
        elif rejection:
            stats["rejected"][rejection] += 1
        self._report_tick_diagnostics(now)

    def _report_tick_diagnostics(self, now):
        if now - self._tick_diagnostics_since < 300:
            return
        symbols = self.active_symbols or tuple(self._tick_diagnostics)
        lines = []
        for symbol in symbols:
            item = self._tick_diagnostics.get(symbol, {})
            def age(timestamp):
                if timestamp is None:
                    return "none"
                return f"{now - timestamp:.1f}s"
            lines.append(
                f"{symbol}: accepted={item.get('accepted', 0)}, "
                f"rejected={dict(item.get('rejected', {}))}, "
                f"received_age={age(item.get('last_received'))}, "
                f"accepted_age={age(item.get('last_accepted'))}, "
                f"quote_age={age(self._quotes.get(symbol))}"
            )
        logger.info(
            "OTC per-pair tick diagnostics (last %.0fs): %s",
            now - self._tick_diagnostics_since, " | ".join(lines)
        )
        self._tick_diagnostics.clear()
        self._tick_diagnostics_since = now

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

    def _clear_quotes(self):
        self._quotes.clear()
        if hasattr(self, "data_readiness"):
            self.data_readiness.reset(self.clock(), "stream_or_session_reset")
        for name in (
            "main_ready",
            "main_broad",
            "groq_preview",
            "cycle_preview",
        ):
            preview = getattr(self, name, None)
            snapshot = getattr(preview, "snapshot", None)
            if snapshot is not None:
                history, assets, _, end = snapshot
                preview.observe(history, assets, {}, end)

    def _price_max_age(self):
        return (
            getattr(self.config, "snapshot_max_age", 30.0)
            if self.transport == "snapshot"
            else self.config.max_tick_age
        )

    def entry_snapshot(self, symbol):
        now = self.clock()
        if (
            not self.running()
            or not 0
            <= now - self._quotes.get(symbol, float("-inf"))
            <= self._price_max_age()
        ):
            return None
        bars = self.closed_history.get(symbol)
        asset = getattr(self.provider, "catalogue", {}).get(symbol)
        if not bars:
            return None
        if asset is not None and symbol in getattr(
            self.provider, "closed_symbols", set()
        ):
            asset = replace(asset, open=False)
        return list(bars), asset

    def running(self):
        return self.task is not None and not self.task.done()

    async def start(self, bot, chat_id, minutes, timeframes):
        if (
            minutes not in (0, 30, 60, 120, 240, 480)
            or not timeframes
            or any(
                t
                not in (
                    TIMEFRAMES
                    + (
                        (120,)
                        if getattr(self.config, "primary_mode", "conditional")
                        == "ready"
                        else ()
                    )
                )
                for t in timeframes
            )
        ):
            raise ValueError("Invalid session selection")
        async with self._lock:
            if self.running():
                return False
            self.chat_id = chat_id
            self.user_stopped = False
            cycles_only = getattr(self.config, "cycles_only", False)
            if hasattr(self, "main_ready"):
                self.main_ready.enabled = (
                    getattr(self.config, "primary_mode", "conditional") == "ready"
                    and not cycles_only
                )
                self.main_ready.allowed_timeframes = (300,)
                self.main_ready.sent_count = 0
            if hasattr(self, "main_broad"):
                self.main_broad.enabled = (
                    getattr(self.config, "primary_mode", "conditional") == "ready"
                    and not cycles_only
                )
                self.main_broad.allowed_timeframes = (300,)
                self.main_broad.sent_count = 0
            if hasattr(self, "groq_preview"):
                self.groq_preview.enabled = (
                    getattr(self.config, "groq_enabled", False) and not cycles_only
                )
            if hasattr(self, "cycle_preview"):
                self.cycle_preview.enabled = cycles_only
                self.cycle_preview.allowed_timeframes = tuple(timeframes)
            self.timeframes = tuple(timeframes)
            self.ends_at = self.clock() + minutes * 60 if minutes else None
            self.session_minutes = minutes
            self.last_tick = None
            self._clear_quotes()
            self.last_error = None
            self.transport = "pending"
            self.poll_requests_used = 0
            self.last_signal_at = None
            self.session_signal_count = 0
            self.session_plan_count = 0
            self._health_counts.clear()
            self._repair_last.clear()
            self._repair_count = 0
            self._repair_window = self.clock()
            self._rotation_selected_at.clear()
            self._rotation_last_removed.clear()
            self._rotation_last_candle.clear()
            self._rotation_pending = None
            self._rotation_next_at = 0
            self.active_symbols = ()
            self.rotations = 0
            self.available = 0
            self.phase = "подключение"
            self.task = asyncio.create_task(self._timed_run(bot))
            return True

    async def stop(self):
        async with self._lock:
            if hasattr(self, "groq_preview"):
                await self.groq_preview.stop()
            if hasattr(self, "main_ready"):
                await self.main_ready.stop()
            if hasattr(self, "main_broad"):
                await self.main_broad.stop()
            if hasattr(self, "cycle_preview"):
                await self.cycle_preview.stop()
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
                self.active_symbols = ()
                self._rotation_pending = None
                self._clear_quotes()
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
            f"\nДо: {end}\nОтслеживается: {monitored} из {self.available}."
            + (f"\nПары в потоке: {', '.join(self.active_symbols)} · замен за сеанс: {self.rotations}" if self.active_symbols else "")
            + f"\n{'С закрытия свечи' if self.transport == 'snapshot' else 'Возраст котировки'}: {age}"
            "\nЭКСПЕРИМЕНТАЛЬНЫЙ РЕЖИМ. Совпадение с платформой не подтверждено."
            + (
                f"\nТариф: {self.plan.plan_name}; осталось запросов: {self.plan.remaining} из {self.plan.quota} за {self.plan.per}; потоки: {self.plan.streams_open}/{self.plan.streams_limit}; активов/поток: {self.plan.instruments_per_stream if self.plan.instruments_per_stream is not None else 'без лимита'}. Лимиты получены при последней проверке."
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

    def catalogue_age(self):
        observed = [
            a.observed_at
            for a in getattr(self.provider, "catalogue", {}).values()
            if a.observed_at is not None
        ]
        return max(0, self.clock() - max(observed)) if observed else None

    def brief_status(self):
        now = self.clock()
        monitored = sum(
            0 <= now - t <= self._price_max_age() for t in self._quotes.values()
        )
        metadata_age = self.catalogue_age()
        metadata_ok = not getattr(self.config, "require_paid_metadata", False) or (
            metadata_age is not None and metadata_age <= self.config.payout_max_age
        )
        active = self.running() and monitored > 0 and metadata_ok
        end = (
            datetime.fromtimestamp(self.ends_at, MOSCOW).strftime("%H:%M")
            if self.ends_at
            else "до остановки"
        )
        frames = " / ".join(f"M{x // 60}" for x in self.timeframes)
        count = (
            self.session_plan_count
            if self.conditional_mode
            else self.session_signal_count
        )
        count_text = f"Планов {count}"
        if getattr(self.config, "primary_mode", "conditional") == "ready" and hasattr(
            self, "main_ready"
        ):
            count_text = f"Основной · сигналов за сеанс {self.main_ready.sent_count}"
        text = f"{'🟢' if active else '🟠'} OTC · {'работает' if active else self.phase}\n{frames} · инструментов {monitored}\n{count_text} · до {end} МСК"
        if hasattr(self, "groq_preview"):
            text += "\n" + self.groq_preview.client.status()
        if self.running() and not metadata_ok:
            text += "\nВыплаты устарели · сигналы на паузе"
        return text

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
        if getattr(self.config, "compact_messages", False):
            metadata_age = self.catalogue_age()
            metadata_ok = not getattr(self.config, "require_paid_metadata", False) or (
                metadata_age is not None and metadata_age <= self.config.payout_max_age
            )
            healthy = (
                self.last_tick is not None
                and 0 <= self.clock() - self.last_tick <= self._price_max_age()
                and metadata_ok
            )
            previous = getattr(self, "_reported_healthy", None)
            self._reported_healthy = healthy
            if not healthy and previous is not False:
                await self._notify(
                    bot,
                    "🟠 Нет свежих данных о выплатах. Сигналы на паузе; повторяю запросы."
                    if not metadata_ok
                    else "🟠 Нет свежих котировок. Переподключаюсь; новые сигналы на паузе.",
                )
            elif healthy and previous is False:
                await self._notify(bot, "🟢 Котировки восстановлены. Бот работает.")
            return
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
            "stream_slot_busy": "единственный поток OTCharts ещё занят",
            "sqlite_busy": "временная занятость SQLite",
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
            # Diagnostic traceback is local and redacted by SecretFormatter.
            # The owner sees a safe error category, never private SQL values.
            logger.exception("OTC stopped by unexpected failure: %s", type(exc).__name__)
            await self._notify(bot, self.last_error)
        finally:
            watchdog.cancel()
            health_watchdog.cancel()
            await asyncio.gather(watchdog, health_watchdog, return_exceptions=True)
            self.phase = "остановлен"
            self.monitored = 0
            self.active_symbols = ()
            self._rotation_pending = None
            self._clear_quotes()
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

    async def _wait_rotation_slot(self):
        """Wait for a free slot or an old stream this process may replace.

        OTCharts keeps a disconnected slot visible until its lease expires.
        After 60 seconds it permits a new SSE to replace the old one; a
        too-young stream instead returns 429 with Retry-After.
        """
        for _ in range(3):
            if self._can_replace_own_stream():
                logger.info("OTC prior stream is older than 60 s; retrying SSE")
                return
            await self._wait_stream_release()
            plan = await self._usage_with_retry()
            self.plan = plan
            if plan.streams_open < plan.streams_limit:
                return
        if self._can_replace_own_stream():
            return
        raise RetryableFeedError()

    def _can_replace_own_stream(self):
        opened = getattr(self.provider, "stream_opened_at", None)
        return (
            opened is not None
            and not getattr(self.provider, "connected", False)
            and time.monotonic() - opened >= 60
        )

    async def _catalogue_with_retry(self):
        for attempt in range(3):
            try:
                return await self.provider.symbols()
            except (RetryableFeedError, TimeoutError) as exc:
                if attempt == 2:
                    raise
                delay = reconnect_delay(attempt, getattr(exc, "retry_after", 0))
                logger.warning(
                    "OTCharts catalogue startup retry %d/3 after %.1fs (%s)",
                    attempt + 2, delay,
                    getattr(exc, "reason", "timeout"),
                )
                await asyncio.sleep(delay)

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
        now = self.clock()
        self._rotation_selected_at = {a.symbol: now for a in chosen}
        self._rotation_next_at = now + max(
            300, getattr(self.config, "pair_rotation_seconds", 600)
        )
        self.active_symbols = tuple(a.symbol for a in chosen)
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
            reserve = max(100, getattr(self.config, "pair_rotation_quota_reserve", 100))
            if plan.remaining <= reserve:
                await self._event("history_repair_quota", symbol)
                return
            fresh = await self.provider.candles(symbol, self.clock())
            fresh = normalize_candles([vars(b) for b in fresh], int(self.clock()))
            if not fresh:
                await self._event("history_repair_empty", symbol)
                return
            # A partial provider response must never discard already observed
            # closed candles. Prefer canonical OHLC for overlapping timestamps.
            # Missing minutes are NEVER synthesized; downstream guards remain strict.
            combined = reconcile_closed_history(
                history.get(symbol, []), fresh, self.clock()
            )
            history[symbol] = combined
            consecutive = contiguous_tail_minutes(combined)
            logger.info(
                "OTC history reconciliation: %s, %d closed bars, "
                "continuous tail %d min (M15 requires at least 900)",
                symbol, len(combined), consecutive,
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
        if not HISTORY_RECOVERY_ENABLED:
            return
        now = self.clock()
        # Bound request use without permanently disabling repair in long sessions.
        if now - self._repair_window >= 3600:
            self._repair_window = now
            self._repair_count = 0
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

    async def _rotation_activity(self, now):
        paths = []
        for name, table in (
            ("cycle_preview", "predictions"),
            ("main_ready", "predictions"),
            ("main_broad", "predictions"),
            ("live_strategies", "signals"),
        ):
            journal = getattr(getattr(self, name, None), "journal", None)
            if journal is not None and getattr(journal, "path", None):
                paths.append((journal.path, table))
        activity = await asyncio.to_thread(journal_activity, paths, now)
        if activity is None:
            return None
        protected, recent = activity
        if self.db:
            protected.update(r["symbol"] for r in await db_call(self.db.pending))
        return protected, recent

    async def _prepare_rotation(self, catalogue, chosen):
        now = self.clock()
        if (
            not getattr(self.config, "pair_rotation_enabled", True)
            or not self._rotation_selected_at
            or self._rotation_pending is not None
            or now < self._rotation_next_at
            or len(catalogue) <= len(chosen)
        ):
            return
        interval = max(300, getattr(self.config, "pair_rotation_seconds", 600))
        self._rotation_next_at = now + interval
        activity = await self._rotation_activity(now)
        if activity is None:
            return
        protected, recent = activity
        swap = choose_pair_swap(
            catalogue, chosen, now, self._rotation_selected_at,
            self._quotes, self._rotation_last_candle, protected, recent,
            self._rotation_last_removed,
        )
        if swap is None:
            return
        victim, asset, reason = swap
        try:
            # Usage inspection is documented as free. Never spend the last
            # portion of the daily request allowance on exploring other pairs.
            quota = await self.provider.usage()
            if quota.remaining <= max(
                100, getattr(self.config, "pair_rotation_quota_reserve", 100)
            ):
                logger.info("OTC pair rotation deferred: request reserve is low")
                return
            candles = await self.provider.candles(asset.symbol, self.clock())
            if not usable_history(candles, self.clock()):
                logger.info("OTC pair rotation skipped %s: history not current", asset.symbol)
                return
            self._rotation_pending = (victim, asset, candles, reason, self.clock())
            logger.info("OTC pair rotation prepared: %s -> %s (%s)",
                        victim, asset.symbol, reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("OTC pair rotation deferred: %s", type(exc).__name__)

    async def _commit_rotation(self, chosen, history):
        proposal = self._rotation_pending
        if proposal is None:
            return None
        victim, asset, candles, reason, prepared_at = proposal
        now = self.clock()
        if now - prepared_at > 60 or victim not in chosen or asset.symbol in chosen:
            self._rotation_pending = None
            return None
        # Avoid the 15-20s pre-entry publishing window and keep stream swaps
        # away from the close/open of a minute where an expiry tick is needed.
        if not 5 <= now % 60 <= 25:
            return None
        activity = await self._rotation_activity(now)
        if activity is None or victim in activity[0]:
            return None
        if not usable_history(candles, now):
            self._rotation_pending = None
            return None
        replacement = dict(chosen)
        replacement.pop(victim)
        replacement[asset.symbol] = asset
        history.pop(victim, None)
        history[asset.symbol] = list(candles)
        self._rotation_selected_at.pop(victim, None)
        self._rotation_selected_at[asset.symbol] = now
        self._rotation_last_removed[victim] = now
        self._rotation_last_candle.pop(victim, None)
        self._rotation_pending = None
        self.active_symbols = tuple(replacement)
        self.rotations += 1
        logger.info("OTC pair rotated: %s -> %s (%s); active=%s",
                    victim, asset.symbol, reason, ",".join(replacement))
        return replacement

    async def _refresh_stream_assets(self, active, chosen):
        # The SSE stream runs independently; catalogue retries must never block
        # consumption of live ticks, nor relax the original 60s metadata TTL.
        interval = float(getattr(self.config, "catalogue_refresh_seconds", 45))
        next_delay = interval
        failures = 0
        while True:
            await asyncio.sleep(next_delay)
            next_delay = interval
            try:
                # Only the symbols endpoint refreshes payout/open evidence.
                # A separate usage check is not required on every refresh.
                catalogue = self._eligible(
                    await self.provider.symbols(),
                    self.clock(),
                    self.config.payout_max_age,
                )
                updated = {a.symbol: a for a in catalogue if a.symbol in chosen}
                active.clear()
                active.update(updated)
                self.available = len(catalogue)
                if failures:
                    logger.info(
                        "OTC catalogue recovered after %d transient failures",
                        failures,
                    )
                failures = 0
                await self._prepare_rotation(catalogue, chosen)
            except AccessUnavailable:
                # Auth, access or quota refusal must stop; never use an old
                # catalogue as a workaround for explicit access denial.
                active.clear()
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Preserve only still-valid observations. Existing per-signal
                # checks expire quotes after 8s and metadata after 60s.
                transient = isinstance(
                    exc, (RetryableFeedError, TimeoutError, ConnectionError)
                )
                if transient:
                    failures += 1
                    # At most two accelerated attempts per outage. Do not spin
                    # against an unhealthy provider or consume the API quota.
                    if failures <= 2:
                        backoff = 5 if failures == 1 else 10
                        requested = getattr(exc, "retry_after", 0)
                        next_delay = max(backoff, requested or 0)
                    # When the quota is known to be near reserve, never
                    # accelerate queries even on a temporary network error.
                    plan = self.plan
                    reserve = max(
                        100, getattr(self.config, "pair_rotation_quota_reserve", 100)
                    )
                    if plan is not None and plan.remaining <= reserve + 10:
                        next_delay = max(interval, next_delay)
                else:
                    failures = 0
                logger.warning(
                    "OTC catalogue refresh failed: %s (%s); retry in %.1fs; "
                    "cached metadata valid only within original 60s TTL",
                    type(exc).__name__,
                    getattr(exc, "reason", "temporary" if transient else "unexpected"),
                    next_delay,
                )
                await self._event("catalogue_refresh_failed")

    async def _scan(self, bot, history, chosen):
        attempt = 0
        builder = MinuteBuilder(self.config.max_tick_age, self.config.max_tick_gap)
        active = dict(chosen)
        self.closed_history = history
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
                                self._record_tick_diagnostic(
                                    tick.symbol, now, rejection=builder.rejection
                                )
                                await self._event(builder.rejection, tick.symbol)
                                continue
                            # Different assets may arrive interleaved. A valid
                            # older tick for B must not move global freshness
                            # backward after a newer valid tick for A.
                            self.last_tick = max(self.last_tick or tick.time, tick.time)
                            self._quotes[tick.symbol] = tick.time
                            self._record_tick_diagnostic(tick.symbol, now, accepted=True)
                            send_quotes = self._quotes
                            if hasattr(self, "data_readiness"):
                                send_quotes = self.data_readiness.observe(
                                    tick, now, history, active, self._quotes
                                )
                            if hasattr(self, "cycle_preview"):
                                self.cycle_preview.observe(
                                    history, active, send_quotes, self.ends_at
                                )
                                self.cycle_preview.observe_tick(tick, now)
                            if hasattr(self, "groq_preview"):
                                self.groq_preview.observe(
                                    history, active, send_quotes, self.ends_at
                                )
                            if (
                                hasattr(self, "main_ready")
                                and getattr(self.config, "primary_mode", "conditional")
                                == "ready"
                            ):
                                self.main_ready.observe(
                                    history, active, send_quotes, self.ends_at
                                )
                                self.main_ready.observe_tick(tick, now)
                                if hasattr(self, "main_broad"):
                                    self.main_broad.observe(
                                        history, active, send_quotes, self.ends_at
                                    )
                                    self.main_broad.observe_tick(tick, now)
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
                                replacement = await self._commit_rotation(chosen, history)
                                if replacement is not None:
                                    chosen = replacement
                                    active = dict(chosen)
                                    builder.clear()
                                    self._clear_quotes()
                                    self.last_tick = None
                                    self.monitored = 0
                                    break
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
                                self._rotation_last_candle[tick.symbol] = now
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
                                if (
                                    getattr(self.config, "primary_mode", "conditional")
                                    == "ready"
                                ):
                                    continue
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
                # A planned change closes the old SSE connection before the
                # next one is opened. Give a single-stream tariff time to
                # release its connection slot; this is not an error reconnect.
                await self._wait_rotation_slot()
            except asyncio.CancelledError:
                raise
            except AccessUnavailable:
                raise
            except Exception as exc:
                if isinstance(exc, sqlite3.OperationalError):
                    if not sqlite_busy(exc):
                        logger.exception("OTC SQLite schema/storage failure; stop safely")
                        raise
                    logger.warning("OTC stream hit a temporary SQLite lock; recovering")
                if refresh_task.done() and not refresh_task.cancelled():
                    refresh_error = refresh_task.exception()
                    if isinstance(refresh_error, AccessUnavailable):
                        raise refresh_error
                # Explain outages before clearing the actual last per-pair quotes.
                # Does not change the watchdog or signal-quality requirements.
                if isinstance(exc, TimeoutError):
                    newest = max(self._quotes.values(), default=None)
                    age = (self.clock() - newest) if newest is not None else None
                    logger.warning(
                        "OTC fresh-tick watchdog expired: newest_quote_age=%s, "
                        "tracked_symbols=%d; reconnecting with gates closed",
                        f"{age:.1f}s" if age is not None else "none",
                        len(self._quotes),
                    )
                self.phase = "Переподключение"
                self.monitored = 0
                self.last_tick = None
                builder.clear()
                self._clear_quotes()
                active = {}
                # Broken continuity invalidates outstanding observations.
                if self.db and not sqlite_busy(exc):
                    await db_call(self.db.recover)
                reason = (
                    "sqlite_busy"
                    if sqlite_busy(exc)
                    else "stream_slot_busy"
                    if isinstance(exc, StreamSlotBusy)
                    else "stream_no_fresh_ticks"
                    if isinstance(exc, TimeoutError)
                    else "stream_disconnected"
                )
                logger.warning("OTC reconnect: %s (%s)", reason, type(exc).__name__)
                if hasattr(self, "data_readiness"):
                    self.data_readiness.reset(self.clock(), reason)
                if sqlite_busy(exc):
                    # If SQLite is busy, do not require an event write to
                    # succeed before the feed is allowed to reconnect.
                    self._health_counts[reason] += 1
                else:
                    await self._event(reason)
                delay = reconnect_delay(attempt, getattr(exc, "retry_after", 0))
                attempt += 1
                await asyncio.sleep(delay)
                active = await self._restore_stream_history(chosen, history)

    async def _restore_stream_history(self, chosen, history):
        attempt = 0
        while True:
            try:
                plan = await self._usage_with_retry()
                self.plan = plan
                # A closed stream can retain the sole Lite slot briefly.
                # Check it before spending paid requests on catalogue/history;
                # an old stream owned here may be replaced after 60 seconds.
                if plan.streams_open >= plan.streams_limit:
                    await self._wait_rotation_slot()
                now = self.clock()
                # The archive remains valid across short disconnects within
                # the same minute. Do not spend 5 candle calls every reconnect.
                missing = [
                    symbol for symbol in chosen
                    if not self._can_reuse_stream_history(history.get(symbol), now)
                ]
                if not HISTORY_RECOVERY_ENABLED:
                    missing = []  # Do not backfill gaps after stream reconnect.
                reserve = max(100, getattr(self.config, "pair_rotation_quota_reserve", 100))
                if missing and plan.remaining < len(missing) + reserve + 2:
                    logger.warning(
                        "OTC history backfill skipped: available=%d, reserve=%d, missing=%d",
                        plan.remaining, reserve, len(missing),
                    )
                    missing = []  # Keep streaming; never bypass history quality checks.
                # The latest catalogue is reusable while its payout/open
                # metadata is safely inside the freshness window. Never use
                # stale cached payouts merely to save a request.
                cached = getattr(self.provider, "catalogue", None)
                max_cached_age = min(30, self.config.payout_max_age / 2)
                if (
                    cached
                    and all(
                        a.observed_at is not None
                        and 0 <= now - a.observed_at <= max_cached_age
                        for a in cached.values()
                    )
                ):
                    catalogue = list(cached.values())
                else:
                    catalogue = await self.provider.symbols()
                if missing:
                    # Bound network parallelism, cancel siblings on failure,
                    # and never open another SSE while a fetch is incomplete.
                    gate = asyncio.Semaphore(2)

                    async def restore_one(symbol):
                        async with gate:
                            return await self.provider.candles(symbol, self.clock())

                    try:
                        async with asyncio.TaskGroup() as group:
                            tasks = {
                                symbol: group.create_task(restore_one(symbol))
                                for symbol in missing
                            }
                    except ExceptionGroup as errors:
                        # Do not hide permission/quota refusals inside a generic
                        # task-group error and retry them forever.
                        errors_list = list(errors.exceptions)
                        denial = next(
                            (exc for exc in errors_list if isinstance(exc, AccessUnavailable)),
                            None,
                        )
                        if denial is not None:
                            raise denial from None
                        transient = next(
                            (exc for exc in errors_list if isinstance(exc, (RetryableFeedError, TimeoutError))),
                            None,
                        )
                        if transient is not None:
                            raise transient from None
                        lock = next(
                            (exc for exc in errors_list if sqlite_busy(exc)), None
                        )
                        if lock is not None:
                            raise lock from None
                        raise
                    # Never erase valid previously seen bars when the provider
                    # returns only a partial page after reconnecting.
                    for symbol, task in tasks.items():
                        history[symbol] = reconcile_closed_history(
                            history.get(symbol, []), task.result(), self.clock()
                        )
                        logger.info(
                            "OTC reconnect history: %s, %d closed bars, "
                            "continuous tail %d min",
                            symbol, len(history[symbol]),
                            contiguous_tail_minutes(history[symbol]),
                        )
                    logger.info(
                        "OTC reconnect history refreshed: %d/%d instruments",
                        len(missing), len(chosen),
                    )
                else:
                    logger.info("OTC reconnect: no history backfill needed or permitted; gaps remain invalid")
                return {
                    a.symbol: a
                    for a in self._eligible(
                        catalogue, self.clock(), self.config.payout_max_age
                    )
                    if a.symbol in chosen
                }
            except (RetryableFeedError, TimeoutError) as exc:
                self.phase = "Переподключение"
                logger.warning("OTC recovery deferred after temporary network failure")
                await self._event("history_reconnect_retry")
                await asyncio.sleep(
                    reconnect_delay(attempt, getattr(exc, "retry_after", 0))
                )
                attempt += 1
            except sqlite3.OperationalError as exc:
                if not sqlite_busy(exc):
                    raise
                self.phase = "Переподключение"
                logger.warning("OTC reconnect history: SQLite busy; deferring safely")
                self._health_counts["history_sqlite_retry"] += 1
                await asyncio.sleep(reconnect_delay(attempt))
                attempt += 1

    @staticmethod
    def _can_reuse_stream_history(bars, now):
        """Reuse only complete continuous history ending this minute."""
        return bool(
            bars and usable_history(bars, now)
            and 0 <= now - (bars[-1].time + 60) <= 60
        )

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
        from services.otc_confirmations import build, format_confirmations

        quality["confirmations"] = build(
            self.closed_history.get(tick.symbol, []), direction, seconds, closed_at
        )
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
            + "\n"
            + format_confirmations(quality["confirmations"])
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
