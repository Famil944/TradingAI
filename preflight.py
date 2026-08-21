import asyncio

from config.settings import settings
from database.db import Database
from exchange.binance_client import BinanceClient


async def check_spot():
    async with BinanceClient() as client:
        ticker = await client.get_ticker("BTCUSDT")
    if not ticker or ticker["price"] <= 0:
        raise RuntimeError("Binance Spot API недоступен")
    return ticker["price"]


def main():
    Database().init_db()
    if not settings.telegram_bot_token or ":" not in settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не настроен")
    btc_price = asyncio.run(check_spot())
    print("Database: OK")
    print("Telegram token: configured")
    print(f"Binance Spot API: OK (BTCUSDT={btc_price})")
    print("Preflight completed. No orders were submitted.")


if __name__ == "__main__":
    main()
