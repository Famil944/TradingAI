"""TradingAI: read-only OTC research bot."""

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import time

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BotCommand
from aiohttp import ClientSession

from bot import otc_commands
from config.settings import ROOT, settings
from core.logging import SecretFormatter
from core.single_instance import SingleInstance
from database.db import Database
from services.otc_data import OTChartsProvider
from services.otc_entry_checks import EntryChecks
from services.otc_evaluation import configured as configured_evaluator
from services.otc_groq import GroqClient, GroqPreview
from services.otc_market_archive import MarketArchive
from services.otc_ml.preview import MLPreview, PreviewJournal
from services.otc_ml.runtime import RuntimeML
from services.otc_preflight import PreflightChecks
from services.otc_ready import ReadyPrimary
from services.otc_session import OTCSession
from services.otc_supervisor import heartbeat
from web.mini_app import start_server

COMMANDS = [
    ("start", "Начать"),
    ("help", "Справка"),
    ("otc", "OTC меню"),
    ("otc_stats", "Виртуальная статистика"),
    ("otc_research", "Качество фильтров и задержек"),
    ("otc_filters", "Проверка рыночных фильтров"),
    ("otc_compare", "Сравнить 3 стратегии"),
    ("otc_m1", "M1: строгий фильтр и статистика"),
    ("otc_panel", "Панель Mini App"),
]


async def initialize_telegram(bot):
    for attempt in range(3):
        try:
            await bot.set_my_commands(
                [BotCommand(command=c, description=d) for c, d in COMMANDS],
                request_timeout=10,
            )
            return await bot.get_me(request_timeout=10)
        except (TelegramNetworkError, TimeoutError):
            if attempt == 2:
                raise
            logging.getLogger(__name__).warning(
                "Telegram startup retry %d/3", attempt + 2
            )
            await asyncio.sleep(2)


async def main(*, session_minutes=None, session_end=None):
    if not settings.telegram_bot_token:
        print("Не задан TELEGRAM_BOT_TOKEN. Заполните локальный .env по .env.example.")
        return 1
    if settings.admin_chat_id <= 0:
        print("Не задан положительный OTC_ADMIN_CHAT_ID личного чата владельца.")
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
    heartbeat_task = asyncio.create_task(heartbeat(service))
    proxy = os.getenv("TRADINGAI_HTTP_PROXY")
    session = None
    if proxy:
        session = AiohttpSession()
        session._session = ClientSession(proxy=proxy)
        session._should_reset_connector = False
    bot = Bot(token=settings.telegram_bot_token, session=session)
    dp = Dispatcher()
    dp.include_router(otc_commands.router)
    evaluator = configured_evaluator(db, bot, settings, ROOT)
    evaluation_task = asyncio.create_task(evaluator.run())
    ml_runtime = RuntimeML(ROOT / "data" / "ml", ROOT / "data" / "ml_provider")
    service.ml_runtime = ml_runtime
    entry_checks = EntryChecks(
        db,
        OTChartsProvider(settings.otcharts_api_key),
        bot,
        settings.admin_chat_id,
        ml=ml_runtime,
        source_snapshot=service.entry_snapshot,
    )
    entry_task = asyncio.create_task(entry_checks.run())
    preflight = PreflightChecks(db, bot, settings.admin_chat_id, service.entry_snapshot)
    preflight_task = asyncio.create_task(preflight.run())
    preview = MLPreview(
        PreviewJournal(ROOT / "data" / "ml-preview.db"),
        RuntimeML(
            ROOT / "data" / "ml_preview20_verified", ROOT / "data" / "ml_preview20"
        ),
        settlement_provider=evaluator.provider,
    )
    await asyncio.to_thread(preview.journal.cancel_future, time.time())
    service.ml_preview = preview
    preview_task = asyncio.create_task(preview.run(bot, settings.admin_chat_id))
    groq_journal = PreviewJournal(ROOT / "data" / "groq-preview.db", label="Groq")
    groq = GroqPreview(
        groq_journal,
        GroqClient(
            settings.groq_api_key,
            groq_journal.path,
            model=settings.groq_model,
            interval=settings.groq_interval_seconds,
            max_requests=settings.groq_max_requests,
            max_tokens=settings.groq_max_tokens,
        ),
        settlement_provider=evaluator.provider,
    )
    await asyncio.to_thread(groq_journal.cancel_future, time.time())
    service.groq_preview = groq
    groq_task = asyncio.create_task(groq.run(bot, settings.admin_chat_id))
    ready_journal = PreviewJournal(
        ROOT / "data" / "primary-ready.db", label="Основной", per_horizon=True
    )
    ready = ReadyPrimary(ready_journal, settlement_provider=evaluator.provider)
    await asyncio.to_thread(ready_journal.cancel_future, time.time())
    service.main_ready = ready
    ready_task = asyncio.create_task(ready.run(bot, settings.admin_chat_id))
    (ROOT / "logs").mkdir(exist_ok=True)
    ml_processes = []
    # Expensive offline collectors are optional, especially on Android.
    if os.getenv("OTC_ML_TRAINING", "false").lower() in ("1", "true", "yes"):
        for name, extra in (("ml-collector", []), ("ml-preview-collector", ["--preview"])):
            with (
                (ROOT / "logs" / f"{name}.stdout.log").open("a", encoding="utf-8") as out,
                (ROOT / "logs" / f"{name}.stderr.log").open("a", encoding="utf-8") as err,
            ):
                try:
                    child = subprocess.Popen(
                        [sys.executable, "-u", "-m", "services.otc_ml.collector", *extra],
                        cwd=ROOT, stdout=out, stderr=err,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    ml_processes.append(child)
                except OSError as exc:
                    logging.getLogger(__name__).warning(
                        "ML collector %s did not start: %s", name, type(exc).__name__
                    )
    mini_app_runner = None
    try:
        if settings.mini_app_enabled:
            mini_app_runner = await start_server(
                service,
                settings.telegram_bot_token,
                settings.admin_chat_id,
                settings.mini_app_port,
            )
            logging.getLogger(__name__).info(
                "Read-only Mini App listening on loopback port %d",
                settings.mini_app_port,
            )
        identity = await initialize_telegram(bot)
        logging.getLogger(__name__).info(
            "Telegram bot @%s initialized; waiting for /otc (test mode: %s).",
            identity.username,
            settings.test_mode,
        )
        if session_minutes is not None:
            await service.start(
                bot, settings.admin_chat_id, session_minutes, (60, 300, 900)
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
        if mini_app_runner is not None:
            await mini_app_runner.cleanup()
        ready_task.cancel()
        await asyncio.gather(ready_task, return_exceptions=True)
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        groq_task.cancel()
        await asyncio.gather(groq_task, return_exceptions=True)
        preflight_task.cancel()
        await asyncio.gather(preflight_task, return_exceptions=True)
        preview_task.cancel()
        await asyncio.gather(preview_task, return_exceptions=True)
        entry_task.cancel()
        await asyncio.gather(entry_task, return_exceptions=True)
        evaluation_task.cancel()
        await asyncio.gather(evaluation_task, return_exceptions=True)
        await service.stop()
        await service.provider.close()
        await bot.session.close()
        # Reap children started by this process so restarting Termux does not
        # leave expensive collector loops running in the background.
        for child in ml_processes:
            if child.poll() is None:
                try:
                    child.terminate()
                except ProcessLookupError:
                    pass
        for child in ml_processes:
            try:
                await asyncio.to_thread(child.wait, timeout=3)
            except subprocess.TimeoutExpired:
                if child.poll() is None:
                    child.kill()
                await asyncio.to_thread(child.wait)
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
