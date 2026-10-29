"""Async iteration over the official OTCharts SDK stream."""
import asyncio
from otcharts import Client


def next_tick(source):
    try:
        return next(source)
    except StopIteration:
        return None


async def ticks(symbols):
    source = iter(Client().stream("otc", symbols, reconnect=False))
    while True:
        tick = await asyncio.to_thread(next_tick, source)
        if tick is None:
            break
        yield tick
