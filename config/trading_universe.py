"""Conservative crypto universe for automated trading."""

AUTO_TRADING_SYMBOLS = {
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "LINKUSDT",
    "AVAXUSDT",
}


def is_auto_trading_symbol(symbol):
    return str(symbol).upper() in AUTO_TRADING_SYMBOLS
