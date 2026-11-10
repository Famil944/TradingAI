"""TradingAI: read-only OTC research bot."""

import argparse
import asyncio
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from bot import otc_commands
from config.settings import settings
from core.logging import SecretFormatter
from core.single_instance import SingleInstance
from database.db import Database
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


async def main(*, session_minutes=None):
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
    service = OTCSession(db=db)
    otc_commands.service = service
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(otc_commands.router)
    try:
        await bot.set_my_commands(
            [BotCommand(command=c, description=d) for c, d in COMMANDS]
        )
        identity = await bot.me()
        logging.getLogger(__name__).info(
            "Telegram bot @%s initialized; waiting for /otc (test mode: %s).",
            identity.username,
            settings.test_mode,
        )
        if session_minutes is not None:
            await service.start(
                bot, settings.admin_chat_id, session_minutes, (60, 300, 900)
            )
            text = (
                "🧪 Условный режим · 30 мин. Прежние планы отменены."
                if service.conditional_mode
                else "🧪 Сеанс запущен. Контроль — раз в 2 минуты. /otc_stop — остановить."
            )
            await service._notify(bot, text)
        await dp.start_polling(bot, close_bot_session=False)
    finally:
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
    args = parser.parse_args()
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
            code = asyncio.run(main(session_minutes=args.otc_session_minutes))
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
