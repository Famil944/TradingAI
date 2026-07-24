import os
from pathlib import Path

from binance.um_futures import UMFutures
from dotenv import load_dotenv

import config.trading_mode as trading_mode
from config.trading_mode import TradingMode
from exchange.binance_futures_client import BinanceFuturesClient


load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class BinanceTestnetClient(BinanceFuturesClient):

    def __init__(self):
        api_key = os.getenv("BINANCE_TESTNET_API_KEY")
        api_secret = os.getenv("BINANCE_TESTNET_API_SECRET")
        if not api_key or not api_secret:
            raise RuntimeError(
                "BINANCE_TESTNET_API_KEY или BINANCE_TESTNET_API_SECRET "
                "не найдены в .env"
            )
        client = UMFutures(
            key=api_key,
            secret=api_secret,
            base_url="https://demo-fapi.binance.com",
        )
        super().__init__(client, self._check_demo_access)

    @staticmethod
    def _check_demo_access():
        if trading_mode.CURRENT_MODE != TradingMode.DEMO:
            raise PermissionError(
                f"Demo-ордер запрещён в режиме "
                f"{trading_mode.CURRENT_MODE.value}"
            )
