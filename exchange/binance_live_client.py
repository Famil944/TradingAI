import os
from pathlib import Path

from binance.um_futures import UMFutures
from dotenv import load_dotenv

import config.trading_mode as trading_mode
from config.trading_mode import LIVE_TRADING_ENABLED, TradingMode
from exchange.binance_futures_client import BinanceFuturesClient
from exchange.portfolio_margin_api import PortfolioMarginApi


load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class BinanceLiveClient(BinanceFuturesClient):

    def __init__(self):
        api_key = (
            os.getenv("BINANCE_LIVE_API_KEY")
            or os.getenv("BINANCE_API_KEY")
        )
        api_secret = (
            os.getenv("BINANCE_LIVE_API_SECRET")
            or os.getenv("BINANCE_API_SECRET")
        )
        if not api_key or not api_secret:
            raise RuntimeError(
                "BINANCE_LIVE_API_KEY или BINANCE_LIVE_API_SECRET "
                "не найдены в .env"
            )
        public_client = UMFutures()
        client = PortfolioMarginApi(
            api_key=api_key,
            api_secret=api_secret,
            public_client=public_client,
        )
        super().__init__(client, self._check_live_access)

    @staticmethod
    def _check_live_access():
        if trading_mode.CURRENT_MODE != TradingMode.LIVE:
            raise PermissionError(
                f"Реальный ордер запрещён в режиме "
                f"{trading_mode.CURRENT_MODE.value}"
            )
        if not LIVE_TRADING_ENABLED:
            raise PermissionError(
                "Live заблокирован: отсутствует точное подтверждение риска"
            )
