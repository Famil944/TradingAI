import asyncio
import threading
from otcharts import Client

async def stream(symbols):
    return Client().stream('otc', symbols)
