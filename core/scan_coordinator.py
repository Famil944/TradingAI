import asyncio


# Один тяжёлый рыночный скан за раз. Основной и Pump-скан используют одну
# блокировку, чтобы не конкурировать за публичный Binance API.
market_scan_lock = asyncio.Lock()
