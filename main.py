"""TradingAI: read-only OTC research bot."""

import argparse
import asyncio
import logging
import os
import time

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)
from aiohttp import ClientSession

from bot import community, otc_commands
from config.settings import ROOT, settings
from core.logging import SecretFormatter
from core.single_instance import SingleInstance
from database.db import Database
from services.otc_data import OTChartsProvider
from services.otc_entry_checks import EntryChecks
from services.otc_evaluation import configured as configured_evaluator
from services.otc_groq import GroqClient
from services.otc_market_archive import MarketArchive
from services.otc_ml.preview import PreviewJournal
from services.otc_preflight import PreflightChecks
from services.otc_ready import ReadyPrimary
from services.otc_session import OTCSession
from services.otc_supervisor import heartbeat
from services.otc_unknown_recovery import UnknownRecovery

COMMANDS = [
    ("start", "Начать"),
    ("help", "Справка"),
    ("otc", "OTC меню"),
    ("otc_stats", "Виртуальная статистика"),
    ("signal_stats", "Винрейт прогнозов"),
    ("suggest", "Предложить улучшение"),
    ("access", "Пользователи и доступ"),
]

# Menu commands are a convenience only; the owner-only middleware is the
# actual authorization boundary. Never advertise trading controls to viewers.
SUBSCRIBER_COMMANDS = [
    ("start", "Запросить доступ"),
    ("help", "О сигналах и рисках"),
    ("signal_stats", "Винрейт S1–S4"),
]


async def initialize_telegram(bot):
    """Keep retrying transient Telegram outages without hiding config errors."""
    public = [BotCommand(command=c, description=d) for c, d in SUBSCRIBER_COMMANDS]
    owner = [BotCommand(command=c, description=d) for c, d in COMMANDS]
    attempt = 0
    while True:
        try:
            # Commands are idempotent; repeat them if a partial setup failed.
            await bot.set_my_commands(
                public, scope=BotCommandScopeDefault(), request_timeout=10
            )
            await bot.set_my_commands(
                public, scope=BotCommandScopeAllPrivateChats(), request_timeout=10
            )
            await bot.set_my_commands(
                owner, scope=BotCommandScopeChat(chat_id=settings.admin_chat_id),
                request_timeout=10,
            )
            return await bot.get_me(request_timeout=10)
        except (TelegramNetworkError, TelegramServerError, TimeoutError) as exc:
            attempt += 1
            delay = min(60, 2 ** min(attempt, 6))
            logging.getLogger(__name__).warning(
                "Telegram temporarily unavailable (%s); retry %d in %ds",
                type(exc).__name__, attempt, delay,
            )
            await asyncio.sleep(delay)


async def main(*, session_minutes=None, session_end=None):
    if not settings.telegram_bot_token:
        print("Не задан TELEGRAM_BOT_TOKEN. Заполните локальный .env по .env.example.")
        return 1
    if settings.admin_chat_id <= 0:
        print("Не задан положительный OTC_ADMIN_CHAT_ID личного чата владельца.")
        return 1
    # Reject unsafe channel targets before any background tasks or collectors run.
    public_channel = os.getenv("OTC_PUBLIC_CHANNEL_ID", "").strip()
    if public_channel and (
        not public_channel.startswith("-100")
        or not public_channel[1:].isdigit()
        or len(public_channel) < 9
    ):
        print("OTC_PUBLIC_CHANNEL_ID должен быть числовым ID канала (-100...).")
        return 1
    db = Database()
    await asyncio.to_thread(db.init_db)
    await asyncio.to_thread(db.recover)
    await asyncio.to_thread(db.close_plans, time.time(), cancel=True)
    archive = MarketArchive(ROOT / "data" / "market-history.db")
    service = OTCSession(
        db=db, provider=OTChartsProvider(settings.otcharts_api_key, archive=archive)
    )
    otc_commands.service = service
    from services.otc_readiness import DataReadiness

    service.data_readiness = DataReadiness(
        ROOT / "data" / "runtime-readiness.db", time.time()
    )
    heartbeat_task = asyncio.create_task(heartbeat(service))
    proxy = os.getenv("TRADINGAI_HTTP_PROXY")
    session = None
    if proxy:
        session = AiohttpSession()
        session._session = ClientSession(proxy=proxy)
        session._should_reset_connector = False
    bot = Bot(token=settings.telegram_bot_token, session=session)
    dp = Dispatcher()
    dp.include_router(community.router)
    dp.include_router(otc_commands.router)
    evaluator = configured_evaluator(db, bot, settings, ROOT)
    evaluation_task = asyncio.create_task(evaluator.run())
    entry_checks = EntryChecks(
        db,
        OTChartsProvider(settings.otcharts_api_key),
        bot,
        settings.admin_chat_id,
        ml=None,
        source_snapshot=service.entry_snapshot,
    )
    entry_task = asyncio.create_task(entry_checks.run())
    preflight = PreflightChecks(db, bot, settings.admin_chat_id, service.entry_snapshot)
    preflight_task = asyncio.create_task(preflight.run())
    groq_client = (
        GroqClient(
            settings.groq_api_key,
            ROOT / "data" / "groq-recovery.db",
            model=settings.groq_model,
            interval=settings.groq_recovery_interval_seconds,
            max_requests=settings.groq_recovery_max_requests,
            max_tokens=settings.groq_recovery_max_tokens,
        )
        if settings.groq_recovery_enabled
        else None
    )
    ready_journal = PreviewJournal(
        ROOT / "data" / "primary-ready.db", label="Основной v9", per_horizon=True
    )
    ready = ReadyPrimary(ready_journal, settlement_provider=evaluator.provider)
    ready.allowed_timeframes = (300,)
    ready.research_timeframes = (120, 300, 900)
    ready.label = "Основной · Строгий M5"
    await asyncio.to_thread(ready_journal.cancel_future, time.time())
    service.main_ready = ready
    ready_task = asyncio.create_task(ready.run(bot, settings.admin_chat_id))
    broad = ReadyPrimary(
        PreviewJournal(
            ROOT / "data" / "primary-not-extended.db",
            label="Без чрезмерного движения M5",
            per_horizon=True,
        ),
        settlement_provider=evaluator.provider,
    )
    broad.policy = "not_extended"
    broad.label = "Основной · Без чрезмерного движения M5"
    broad.allowed_timeframes = (300,)
    await asyncio.to_thread(broad.journal.cancel_future, time.time())
    service.main_broad = broad
    broad_task = asyncio.create_task(broad.run(bot, settings.admin_chat_id))
    from services.otc_bollinger_live import BollingerCycles, CycleJournal
    from services.otc_subscribers import SubscriberActivityMiddleware, SubscriberRelay

    # Only opt-in users with an active hourly session receive new forecasts.
    subscriber_relay = SubscriberRelay(ROOT, require_sessions=True)
    dp.update.outer_middleware(SubscriberActivityMiddleware(subscriber_relay.store))
    cycle_journal = CycleJournal(
        ROOT / "data" / "bollinger-cycles.db", label="Боллинджер ×3", per_horizon=True
    )
    await asyncio.to_thread(cycle_journal.cancel_future, time.time())
    cycles = BollingerCycles(cycle_journal, subscriber_relay=subscriber_relay)
    subscriber_relay.live_cycle = cycles
    service.cycle_preview = cycles
    cycle_task = asyncio.create_task(cycles.run(bot, settings.admin_chat_id))
    from services.otc_strategies_live import LiveStrategyPublisher
    strategy_engine = LiveStrategyPublisher(
        ROOT, subscriber_relay=subscriber_relay
    )
    service.live_strategies = strategy_engine
    strategy_task = asyncio.create_task(
        strategy_engine.run(bot, settings.admin_chat_id, service, cycles)
    )
    recovery = UnknownRecovery(
        ROOT,
        service.provider,
        groq_client,
        interval=settings.groq_recovery_interval_seconds,
        foreground=service,
        quota_reserve=settings.pair_rotation_quota_reserve,
    )
    service.unknown_recovery = recovery
    # Historical UNKNOWN backfills are opt-in: preserve API quota by default.
    recovery_task = (
        asyncio.create_task(recovery.run())
        if os.getenv("OTC_UNKNOWN_RECOVERY_ENABLED", "false").strip().lower()
        in ("1", "true", "yes", "on")
        else None
    )
    # New signals are mirrored immediately after the owner's send succeeds.
    # The relay loop remains a fallback and mirrors later WIN/LOSS edits.
    subscriber_task = asyncio.create_task(subscriber_relay.run(bot))
    # Opt-in public reporting; independent from the private virtual wallet.
    public_task = None
    public_channel = os.getenv("OTC_PUBLIC_CHANNEL_ID", "").strip()
    # Public channel broadcasts bypass all per-user approval/expiry controls.
    # Require an explicit second opt-in to avoid accidentally leaking gated signals.
    if public_channel and os.getenv("OTC_ENABLE_UNGATED_PUBLIC_CHANNEL", "").strip() == "1":
        from services.otc_public_broadcast import PublicPublisher

        public_publisher = PublicPublisher(ROOT, int(public_channel), live_cycle=cycles)
        public_task = asyncio.create_task(public_publisher.run(bot))
    elif public_channel:
        logging.getLogger(__name__).warning(
            "Public OTC channel disabled: it would bypass owner-approved subscriber access. "
            "Explicit OTC_ENABLE_UNGATED_PUBLIC_CHANNEL=1 is required."
        )
    if settings.cycles_only:
        ready.enabled = broad.enabled = False
    (ROOT / "logs").mkdir(exist_ok=True)
    try:
        identity = await initialize_telegram(bot)
        logging.getLogger(__name__).info(
            "Telegram bot @%s initialized; waiting for /otc (test mode: %s).",
            identity.username,
            settings.test_mode,
        )
        if session_minutes is not None:
            await service.start(
                bot, settings.admin_chat_id, session_minutes, (120, 300, 900)
            )
            if session_end is not None:
                service.ends_at = session_end
            text = (
                "🧪 Сеанс обновлён до заданного времени; будущие планы отменены."
                if session_end is not None
                else f"🧪 Условный режим · {session_minutes} мин. Прежние планы отменены."
                if service.conditional_mode
                else "🧪 Сеанс запущен. Контроль — раз в 2 минуты. /otc_stop — остановить."
            )
            indicators = await asyncio.to_thread(otc_commands.strategy_status, service)
            await service._notify(bot, text + "\n\n" + indicators)
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        strategy_task.cancel()
        await asyncio.gather(strategy_task, return_exceptions=True)
        subscriber_task.cancel()
        await asyncio.gather(subscriber_task, return_exceptions=True)
        if public_task is not None:
            public_task.cancel()
            await asyncio.gather(public_task, return_exceptions=True)
        cycle_task.cancel()
        await asyncio.gather(cycle_task, return_exceptions=True)
        broad_task.cancel()
        await asyncio.gather(broad_task, return_exceptions=True)
        ready_task.cancel()
        await asyncio.gather(ready_task, return_exceptions=True)
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        if recovery_task is not None:
            recovery_task.cancel()
            await asyncio.gather(recovery_task, return_exceptions=True)
        if groq_client is not None:
            await groq_client.close()
        preflight_task.cancel()
        await asyncio.gather(preflight_task, return_exceptions=True)
        entry_task.cancel()
        await asyncio.gather(entry_task, return_exceptions=True)
        evaluation_task.cancel()
        await asyncio.gather(evaluation_task, return_exceptions=True)
        await service.stop()
        await service.provider.close()
        await bot.session.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--otc-session-minutes",
        type=int,
        choices=(0, 30, 60, 120, 240, 480),
        default=None,
    )
    parser.add_argument("--otc-session-end", type=float, default=None)
    args = parser.parse_args()
    if args.otc_session_end is not None and (
        args.otc_session_minutes in (None, 0) or args.otc_session_end <= time.time()
    ):
        parser.error(
            "session end requires a finite active session and a future timestamp"
        )
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(
            SecretFormatter(
                (
                    settings.telegram_bot_token,
                    settings.otcharts_api_key,
                    settings.groq_api_key,
                )
            )
        )
    # Framework request URLs may include bot credentials; hide transport details.
    logging.getLogger("aiohttp").setLevel(logging.CRITICAL)
    logging.getLogger("aiogram").setLevel(logging.CRITICAL)
    instance = SingleInstance()
    if not instance.acquire():
        print("Другой экземпляр бота уже запущен.")
        raise SystemExit(2)
    try:
        try:
            code = asyncio.run(
                main(
                    session_minutes=args.otc_session_minutes,
                    session_end=args.otc_session_end,
                )
            )
        except KeyboardInterrupt:
            code = 0
        except Exception as exc:
            logging.error(
                "Запуск остановлен: %s. Проверьте конфигурацию и сеть.",
                type(exc).__name__,
            )
            code = 1
    finally:
        instance.close()
    raise SystemExit(code)
