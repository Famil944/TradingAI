import asyncio
import logging
from aiogram import Dispatcher, Bot
from aiogram.types import BotCommand
from config.settings import settings
from database.db import Database
from bot.commands import router as commands_router
from core.single_instance import SingleInstance
from services.signal_watch_service import SignalWatchService

# Настройка логирования
logging.basicConfig(
    level=settings.log_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def main():
    """Главная функция запуска бота."""
    
    # Инициализация базы данных
    logger.info("Инициализация базы данных...")
    db = Database()
    db.init_db()
    logger.info("✅ База данных инициализирована")
    
    # Создание бота
    logger.info("Создание Telegram бота...")
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    
    # Регистрация роутера команд
    dp.include_router(commands_router)
    
    # Установка команд в меню бота
    commands = [
        BotCommand(command="start", description="Запуск бота"),
        BotCommand(command="signals", description="Текущие сигналы"),
        BotCommand(command="top", description="TOP-10 по Score"),
        BotCommand(command="scan", description="Запустить скан"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="settings", description="Настройки"),
        BotCommand(command="help", description="Справка"),
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception:
        await bot.session.close()
        raise
    
    logger.info("✅ Бот создан и настроен")
    watch_service = SignalWatchService(bot)
    watch_task = asyncio.create_task(watch_service.run())
    
    # Запуск polling'а бота
    logger.info("🚀 Бот запущен в ручном режиме! Ожидание /scan...")
    
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("Остановка бота...")
    finally:
        watch_service.stop()
        watch_task.cancel()
        await asyncio.gather(watch_task, return_exceptions=True)
        await bot.session.close()
        logger.info("✅ Бот остановлен")


if __name__ == "__main__":
    instance = SingleInstance()
    if not instance.acquire():
        logger.error("Бот уже запущен. Второй экземпляр остановлен.")
        raise SystemExit(2)
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
    finally:
        instance.close()
