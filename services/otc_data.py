"""OTCharts market data SDK client."""
import asyncio
from otcharts import Client

async def usage():
    return await asyncio.to_thread(Client().usage)

async def symbols():
    return await asyncio.to_thread(Client().symbols, 'otc')

async def candles(symbol):
    return await asyncio.to_thread(Client().candles, 'otc', symbol, tf=60, limit=1200)
