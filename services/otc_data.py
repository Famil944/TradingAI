"""Async adapter for OTCharts 0.7.0 documented HTTP contract.

SDK source and published metadata verified locally. Pocket Option timestamps are
venue seconds at UTC+2, explicitly converted here, never inferred by proximity.
The live catalogue provides names, payouts and opening flags. Receipt time is
recorded separately: the upstream API does not provide payout timestamps.
"""

import asyncio
import json
import math
import time
from dataclasses import dataclass
from importlib.metadata import version

import os

import aiohttp

from services.otc_market import Asset, Tick, utc_timestamp
from services.otc_strategy import normalize_candles

SDK_VERSION = "0.7.0"
VENUE_OFFSET_SECONDS = 7200


class AccessUnavailable(RuntimeError):
    pass


class RetryableFeedError(RuntimeError):
    def __init__(self, retry_after=0):
        super().__init__(
            "OTCharts временно недоступен: сетевая ошибка или ответ сервера."
        )
        self.retry_after = retry_after


@dataclass(frozen=True)
class Plan:
    plan_name: str
    remaining: int
    quota: int
    per: str
    resets: int
    streams_open: int
    streams_limit: int
    instruments_per_stream: int | None
    books: tuple


def venue_timestamp(value):
    return utc_timestamp(value) - VENUE_OFFSET_SECONDS


async def wait_for_tick_time(tick):
    """Buffer at most two seconds of clock skew; never relabel future data."""
    ahead = tick.time - time.time()
    if 0 < ahead <= 2:
        await asyncio.sleep(ahead)


class OTChartsProvider:
    source = "otcharts"
    # There is no independent platform evidence supplied by this provider.
    synchronized = False

    def __init__(self, api_key, session=None, archive=None):
        self.archive = archive
        self._key = api_key
        self._session = session
        self._owned = session is None
        self.connected = False
        self.payout_evidence = False
        self.closed_symbols = set()
        self.catalogue = {}
        self.usage_snapshot = {}

    async def _http(self):
        if not self._key:
            raise AccessUnavailable(
                "Нет OTCHARTS_API_KEY. Нет доступа к потоковым котировкам/выплатам."
            )
        if version("otcharts") != SDK_VERSION:
            raise AccessUnavailable("Версия OTCharts не проверена; требуется 0.7.0.")
        if self._session is None:
            self._session = aiohttp.ClientSession(
                proxy=os.getenv("TRADINGAI_HTTP_PROXY"),
                headers={"Authorization": "Bearer " + self._key},
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    @staticmethod
    def _check(response):
        messages = {
            401: "OTCharts отклонил API-ключ (HTTP 401). Проверьте OTCHARTS_API_KEY в локальном .env.",
            402: "OTCharts: тариф не разрешает доступ к данным (HTTP 402).",
            403: "OTCharts запретил запрос (HTTP 403): ограничения доступа или тарифа.",
            404: "Запрошенные данные OTCharts не найдены (HTTP 404).",
            429: "OTCharts: исчерпан лимит запросов или соединений (HTTP 429).",
        }
        if response.status in messages:
            raise AccessUnavailable(messages[response.status])
        if response.status == 503:
            try:
                delay = max(1, min(300, int(response.headers.get("Retry-After", "30"))))
            except ValueError:
                delay = 30
            raise RetryableFeedError(delay)
        if response.status != 200:
            raise RetryableFeedError()

    async def _get(self, path, params=None):
        session = await self._http()
        try:
            async with session.get(
                "https://otcharts.com" + path, params=params
            ) as response:
                self._check(response)
                return await response.json()
        except (asyncio.TimeoutError, aiohttp.ClientError):
            # Never expose request headers, URLs or exception bodies containing keys.
            raise RetryableFeedError() from None

    async def usage(self):
        d = await self._get("/v1/usage")
        self.usage_snapshot = d
        req, streams = d.get("requests", {}), d.get("streams", {})
        return Plan(
            str(d.get("planName", "")),
            int(req.get("remaining", 0)),
            int(req.get("quota", 0)),
            str(req.get("per", "day")),
            int(req.get("resets", 0)),
            int(streams.get("open", 0)),
            int(streams.get("limit", 0)),
            streams.get("instrumentsPerStream"),
            tuple(d.get("books", [])),
        )

    async def symbols(self):
        d = await self._get("/v1/symbols", {"venue": "otc"})
        # No invented observation timestamp: response receipt is not payout age.
        observed_at = time.time()
        result = []
        for a in d.get("symbols", []):
            payout = a.get("payout")
            payout = (
                float(payout)
                if isinstance(payout, (float, int)) and not isinstance(payout, bool)
                else None
            )
            if payout is not None and (
                not math.isfinite(payout) or not 0 <= payout <= 100
            ):
                payout = None
            if a.get("open") is False:
                self.closed_symbols.add(str(a["symbol"]))
            elif a.get("open") is True:
                self.closed_symbols.discard(str(a["symbol"]))
            result.append(
                Asset(
                    str(a["symbol"]),
                    a.get("isOtc") is True,
                    a.get("open") if isinstance(a.get("open"), bool) else None,
                    payout,
                    None,
                    a.get("name") if isinstance(a.get("name"), str) else None,
                    observed_at,
                )
            )
        self.catalogue = {a.symbol: a for a in result}
        if self.archive is not None:
            await asyncio.to_thread(self.archive.assets, result, observed_at)
        return result

    async def candles(self, symbol, now):
        d = await self._get(
            "/v1/candles",
            {
                "venue": "otc",
                "symbol": symbol,
                "tf": 60,
                "limit": 5000 if self.archive is not None else 1200,
            },
        )
        if d.get("open") is False:
            self.closed_symbols.add(symbol)
        rows = []
        for row in d.get("candles", []):
            try:
                rows.append({**row, "time": venue_timestamp(row["time"])})
            except (KeyError, ValueError, TypeError):
                continue
        bars = normalize_candles(rows, int(now))
        if self.archive is not None:
            await asyncio.to_thread(self.archive.bars, symbol, bars, time.time())
        return bars[-1200:]

    async def record_closed(self, symbol, bar, now):
        if self.archive is not None:
            await asyncio.to_thread(
                self.archive.bars, symbol, [bar], now, "otcharts_ticks"
            )

    async def ticks(self, symbols):
        session = await self._http()
        timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=15)
        try:
            async with session.get(
                "https://otcharts.com/v1/stream",
                params={"venue": "otc", "symbol": ",".join(symbols)},
                headers={"Accept": "text/event-stream"},
                timeout=timeout,
            ) as response:
                self._check(response)
                closed = response.headers.get("x-otc-closed", "")
                self.closed_symbols.update(
                    x.strip() for x in closed.split(",") if x.strip() in symbols
                )
                event = "message"
                async for raw in response.content:
                    if len(raw) > 65536:
                        raise RetryableFeedError()
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        event = "message"
                        continue
                    if line.startswith("event:"):
                        event = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    try:
                        d = json.loads(line[5:].strip())
                        symbol = str(
                            d.get("symbol", symbols[0] if len(symbols) == 1 else "")
                        )
                        if event == "closed":
                            if symbol in symbols:
                                self.closed_symbols.add(symbol)
                            continue
                        price = d.get("price", d.get("close"))
                        tick = Tick(symbol, venue_timestamp(d["time"]), float(price))
                    except (KeyError, ValueError, TypeError):
                        continue
                    if symbol in symbols:
                        await wait_for_tick_time(tick)
                        yield tick
        finally:
            self.connected = False

    async def close(self):
        self.connected = False
        if self._session is not None and self._owned:
            await self._session.close()
            self._session = None
