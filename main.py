"""TradingAI: read-only OTC research bot."""

import argparse
import asyncio
import logging
import subprocess
import sys
import time

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BotCommand

from bot import otc_commands
from config.settings import ROOT, settings
from core.logging import SecretFormatter
from core.single_instance import SingleInstance
from database.db import Database
from services.otc_data import OTChartsProvider
from services.otc_entry_checks import EntryChecks
from services.otc_evaluation import configured as configured_evaluator
from services.otc_market_archive import MarketArchive
from services.otc_ml.preview import MLPreview, PreviewJournal
from services.otc_ml.runtime import RuntimeML
from services.otc_session import OTCSession

COMMANDS = [
    ("start", "Начать"),
    ("help", "Справка"),
    ("otc", "OTC меню"),
    ("otc_status", "Статус OTC"),
    ("otc_stop", "Остановить OTC"),
    ("otc_stats", "Виртуальная статистика"),
    ("otc_diagnostics", "Диагностика OTC"),
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
    bot = Bot(token=settings.telegram_bot_token)
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
    preview = MLPreview(
        PreviewJournal(ROOT / "data" / "ml-preview.db"),
        RuntimeML(
            ROOT / "data" / "ml_preview20_verified", ROOT / "data" / "ml_preview20"
        ),
    )
    await asyncio.to_thread(preview.journal.cancel_future, time.time())
    service.ml_preview = preview
    preview_task = asyncio.create_task(preview.run(bot, settings.admin_chat_id))
    (ROOT / "logs").mkdir(exist_ok=True)
    with (
        (ROOT / "logs" / "ml-collector.stdout.log").open("a", encoding="utf-8") as out,
        (ROOT / "logs" / "ml-collector.stderr.log").open("a", encoding="utf-8") as err,
    ):
        subprocess.Popen(
            [sys.executable, "-u", "-m", "services.otc_ml.collector"],
            cwd=ROOT,
            stdout=out,
            stderr=err,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    with (
        (ROOT / "logs" / "ml-preview-collector.stdout.log").open(
            "a", encoding="utf-8"
        ) as out,
        (ROOT / "logs" / "ml-preview-collector.stderr.log").open(
            "a", encoding="utf-8"
        ) as err,
    ):
        subprocess.Popen(
            [sys.executable, "-u", "-m", "services.otc_ml.collector", "--preview"],
            cwd=ROOT,
            stdout=out,
            stderr=err,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    try:
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
                service.ends_at = min(service.ends_at, session_end)
            text = (
                "🧪 Обновлено. Сеанс продолжается до прежнего времени; будущие планы отменены."
                if session_end is not None
                else f"🧪 Условный режим · {session_minutes} мин. Прежние планы отменены."
                if service.conditional_mode
                else "🧪 Сеанс запущен. Контроль — раз в 2 минуты. /otc_stop — остановить."
            )
            await service._notify(bot, text)
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        preview_task.cancel()
        await asyncio.gather(preview_task, return_exceptions=True)
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
            SecretFormatter((settings.telegram_bot_token, settings.otcharts_api_key))
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
