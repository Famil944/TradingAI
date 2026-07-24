import config.trading_mode as trading_mode
from config.trading_mode import TradingMode


def create_trading_client():
    if trading_mode.CURRENT_MODE == TradingMode.DEMO:
        from exchange.binance_testnet_client import BinanceTestnetClient

        return BinanceTestnetClient()

    if trading_mode.CURRENT_MODE == TradingMode.LIVE:
        from exchange.binance_live_client import BinanceLiveClient

        return BinanceLiveClient()

    raise RuntimeError(
        f"Торговый клиент недоступен для режима: "
        f"{trading_mode.CURRENT_MODE.value}"
    )
