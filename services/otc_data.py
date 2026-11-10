"""Async adapter for OTCharts 0.7.0 documented HTTP contract.

SDK source and published metadata verified locally. Pocket Option timestamps are
venue seconds at UTC+2, explicitly converted here, never inferred by proximity.
The published Instrument has no payout timestamp and reports open=None for OTC:
those observations are deliberately ineligible for signals.
"""

import json
from dataclasses import dataclass
from importlib.metadata import version

import aiohttp

from services.otc_market import Asset, Tick, utc_timestamp
from services.otc_strategy import normalize_candles

SDK_VERSION = "0.7.0"
VENUE_OFFSET_SECONDS = 7200


class AccessUnavailable(RuntimeError):
    pass


class RetryableFeedError(RuntimeError):
    def __init__(self, retry_after=0):
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


class OTChartsProvider:
    source = "otcharts"
    # There is no independent platform evidence supplied by this provider.
    synchronized = False

    def __init__(self, api_key, session=None):
        self._key = api_key
        self._session = session
        self._owned = session is None
        self.connected = False
        self.payout_evidence = False

    async def _http(self):
        if not self._key:
            raise AccessUnavailable(
                "Нет OTCHARTS_API_KEY. Нет доступа к потоковым котировкам/выплатам."
            )
        if version("otcharts") != SDK_VERSION:
            raise AccessUnavailable("Версия OTCharts не проверена; требуется 0.7.0.")
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": "Bearer " + self._key},
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    @staticmethod
    def _check(response):
        if response.status in (401, 402, 403, 404, 429):
            raise AccessUnavailable("Нет доступа к данным или исчерпан лимит OTCharts.")
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
        async with session.get(
            "https://otcharts.com" + path, params=params
        ) as response:
            self._check(response)
            return await response.json()

    async def usage(self):
        d = await self._get("/v1/usage")
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
        result = []
        for a in d.get("symbols", []):
            payout = a.get("payout")
            payout = (
                float(payout)
                if isinstance(payout, (float, int)) and not isinstance(payout, bool)
                else None
            )
            result.append(
                Asset(
                    str(a["symbol"]),
                    a.get("isOtc") is True,
                    a.get("open") if isinstance(a.get("open"), bool) else None,
                    payout,
                    None,
                )
            )
        return result

    async def candles(self, symbol, now):
        d = await self._get(
            "/v1/candles", {"venue": "otc", "symbol": symbol, "tf": 60, "limit": 1200}
        )
        rows = []
        for row in d.get("candles", []):
            try:
                rows.append({**row, "time": venue_timestamp(row["time"])})
            except (KeyError, ValueError, TypeError):
                continue
        return normalize_candles(rows, int(now))

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
                async for raw in response.content:
                    if len(raw) > 65536:
                        raise RetryableFeedError()
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        d = json.loads(line[5:].strip())
                        symbol = str(
                            d.get("symbol", symbols[0] if len(symbols) == 1 else "")
                        )
                        price = d.get("price", d.get("close"))
                        tick = Tick(symbol, venue_timestamp(d["time"]), float(price))
                    except (KeyError, ValueError, TypeError):
                        continue
                    if symbol in symbols:
                        yield tick
        finally:
            self.connected = False

    async def close(self):
        self.connected = False
        if self._session is not None and self._owned:
            await self._session.close()
            self._session = None
