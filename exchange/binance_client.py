import aiohttp
import asyncio
from typing import List, Dict, Optional
from datetime import datetime
from config.models import CandleData
from config.settings import settings
import logging
import time

logger = logging.getLogger(__name__)


class RateLimiter:
    """Контроль лимита запросов Binance (1200 weight/min)."""
    
    def __init__(self, limit: int = 1200, window: int = 60):
        self.limit = limit
        self.window = window
        self.requests: List[float] = []
        self.lock = asyncio.Lock()
    
    async def acquire(self, weight: int = 1):
        """Получить разрешение на запрос."""
        while True:
            async with self.lock:
                now = datetime.now().timestamp()
                self.requests = [t for t in self.requests if now - t < self.window]
                if len(self.requests) + weight <= self.limit:
                    self.requests.extend([now] * weight)
                    return
                wait_time = max(0.05, self.window - (now - self.requests[0]))
            logger.warning(f"Rate limit reached, waiting {wait_time:.1f}s")
            await asyncio.sleep(wait_time)


class BinanceClient:
    """Клиент для работы с Binance Spot Public API."""
    
    BASE_URL = settings.binance_base_url
    PUBLIC_BASE_URLS = (
        "https://data-api.binance.vision",
        "https://api-gcp.binance.com",
        "https://api1.binance.com",
        "https://api2.binance.com",
        "https://api3.binance.com",
        "https://api4.binance.com",
    )
    RATE_LIMITER = RateLimiter()
    _blocked_base_urls = set()
    _preferred_base_url = None
    _unavailable_until = {}
    _endpoint_cooldown_seconds = 20
    _last_all_unavailable_log = 0.0
    _exchange_info_cache = None
    _exchange_info_cached_at = 0.0
    _exchange_info_ttl_seconds = 3600
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self._endpoint_probe_lock = asyncio.Lock()
        self._exchange_info_lock = asyncio.Lock()
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def _request_from_base(self, method, base_url, endpoint, params):
        url = f"{base_url.rstrip('/')}{endpoint}"
        try:
            async with self.session.request(
                method, url, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return "success", await resp.json()
                if resp.status == 451:
                    first_block = base_url not in type(self)._blocked_base_urls
                    type(self)._blocked_base_urls.add(base_url)
                    if type(self)._preferred_base_url == base_url:
                        type(self)._preferred_base_url = None
                    if first_block:
                        logger.warning(
                            "Binance endpoint %s вернул 451 и исключён из повторных запросов",
                            base_url,
                        )
                    return "blocked", None
                if 400 <= resp.status < 500:
                    logger.error("Binance API error %s: %s", resp.status, await resp.text())
                    return "terminal", None
        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            logger.warning("Binance endpoint %s недоступен: %s", base_url, error)
        return "unavailable", None

    async def _request(self, method: str, endpoint: str, params: Dict = None, weight: int = 1) -> Dict:
        """Выполнить запрос с failover и временным карантином сбойных адресов."""
        await self.RATE_LIMITER.acquire(weight)
        cls = type(self)
        attempted = set()

        async def request_base(base_url):
            attempted.add(base_url)
            status, data = await self._request_from_base(
                method, base_url, endpoint, params
            )
            if status == "unavailable":
                cls._unavailable_until[base_url] = (
                    time.monotonic() + cls._endpoint_cooldown_seconds
                )
                if cls._preferred_base_url == base_url:
                    cls._preferred_base_url = None
            return status, data

        preferred = cls._preferred_base_url
        if preferred and preferred not in cls._blocked_base_urls:
            status, data = await request_base(preferred)
            if status == "success":
                return data
            if status == "terminal":
                return None

        async with self._endpoint_probe_lock:
            # Пока корутина ожидала lock, другая могла уже выбрать рабочий адрес.
            preferred = cls._preferred_base_url
            if (preferred and preferred not in attempted and
                    preferred not in cls._blocked_base_urls):
                status, data = await request_base(preferred)
                if status == "success":
                    return data
                if status == "terminal":
                    return None

            now = time.monotonic()
            bases = list(dict.fromkeys((self.BASE_URL, *self.PUBLIC_BASE_URLS)))
            for base_url in (base for base in bases if base):
                if (base_url in attempted or base_url in cls._blocked_base_urls or
                        cls._unavailable_until.get(base_url, 0) > now):
                    continue
                status, data = await request_base(base_url)
                if status == "success":
                    cls._preferred_base_url = base_url
                    cls._unavailable_until.pop(base_url, None)
                    if base_url != self.BASE_URL:
                        logger.info("Binance fallback endpoint активен: %s", base_url)
                    return data
                if status == "terminal":
                    return None

        now = time.monotonic()
        if now - cls._last_all_unavailable_log >= cls._endpoint_cooldown_seconds:
            logger.error(
                "Все публичные Binance Spot endpoints недоступны; запросы временно приостановлены"
            )
            cls._last_all_unavailable_log = now
        return None
    
    async def get_klines(self, symbol: str, interval: str, limit: int = 100) -> List[CandleData]:
        """Получить свечи."""
        data = await self._request(
            "GET",
            "/api/v3/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "limit": limit
            },
            weight=1
        )
        
        if not data:
            return []
        
        candles = []
        for candle in data:
            candles.append(CandleData(
                timestamp=int(candle[0]),
                open=float(candle[1]),
                high=float(candle[2]),
                low=float(candle[3]),
                close=float(candle[4]),
                volume=float(candle[7]),
                trade_count=int(candle[8]) if len(candle) > 8 else None,
                taker_buy_quote_volume=(
                    float(candle[10]) if len(candle) > 10 else None
                ),
            ))
        
        return candles
    
    async def get_ticker(self, symbol: str) -> Optional[Dict]:
        """Получить текущие данные по паре."""
        data = await self._request(
            "GET",
            "/api/v3/ticker/24hr",
            {"symbol": symbol},
            weight=1
        )
        
        if not data:
            return None

        try:
            return {
                "symbol": data.get("symbol"),
                "price": float(data.get("lastPrice", 0)),
                "price_change": float(data.get("priceChange", 0)),
                "price_change_percent": float(data.get("priceChangePercent", 0)),
                "volume": float(data.get("volume", 0)),
                "quote_asset_volume": float(
                    data.get("quoteVolume", data.get("quoteAssetVolume", 0))
                ),
                "bid_price": float(data.get("bidPrice", 0)),
                "ask_price": float(data.get("askPrice", 0)),
            }
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка парсинга ticker для {symbol}: {e}")
            return None

    async def get_tick_size(self, symbol: str) -> Optional[float]:
        """Получить минимальный шаг цены Binance Spot для пары."""
        symbols = await self.get_spot_symbol_map()
        item = symbols.get(symbol)
        if not item:
            return None
        for rule in item.get("filters", []):
            if rule.get("filterType") == "PRICE_FILTER":
                try:
                    tick_size = float(rule["tickSize"])
                    return tick_size if tick_size > 0 else None
                except (KeyError, TypeError, ValueError):
                    return None
        return None

    async def get_spot_symbol_map(self) -> Dict[str, Dict]:
        """Кэш активных Spot-пар и торговых правил на один час."""
        cls = type(self)
        now = time.monotonic()
        if (cls._exchange_info_cache is not None and
                now - cls._exchange_info_cached_at < cls._exchange_info_ttl_seconds):
            return cls._exchange_info_cache
        async with self._exchange_info_lock:
            now = time.monotonic()
            if (cls._exchange_info_cache is not None and
                    now - cls._exchange_info_cached_at < cls._exchange_info_ttl_seconds):
                return cls._exchange_info_cache
            data = await self._request("GET", "/api/v3/exchangeInfo", {}, weight=20)
        if not isinstance(data, dict):
            return cls._exchange_info_cache or {}
        result = {}
        for item in data.get("symbols", []):
            symbol = item.get("symbol")
            spot_allowed = item.get("isSpotTradingAllowed", True)
            if symbol and item.get("status") == "TRADING" and spot_allowed:
                result[symbol] = item
        if result:
            cls._exchange_info_cache = result
            cls._exchange_info_cached_at = now
        return result
    
    async def get_top_symbols(self, limit: int = 100) -> List[str]:
        """Получить TOP монет по волюму (USDT пары)."""
        data = await self._request(
            "GET",
            "/api/v3/ticker/24hr",
            {},
            weight=40  # Получение всех пар требует 40 weight
        )
        
        if not data:
            return []
        active_symbols = await self.get_spot_symbol_map()
        
        # Эти активы фактически дублируют доллар/фиат и занимают места в TOP,
        # но не подходят для стратегии роста к цели +3%.
        excluded_quote_like_assets = {
            "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDP", "USDD",
            "USD1", "XUSD", "RLUSD", "EURI", "EUR", "AEUR", "PAXG", "XAUT",
        }
        
        pairs = []
        for item in data:
            try:
                symbol = item.get("symbol", "")
                
                if not symbol.endswith("USDT"):
                    continue
                
                if symbol == "USDTUSDT":
                    continue

                if symbol in settings.excluded_symbols:
                    logger.info("%s исключён локальным риск-фильтром", symbol)
                    continue

                if active_symbols and symbol not in active_symbols:
                    continue
                
                if symbol[:-4] in excluded_quote_like_assets:
                    continue
                
                quote_volume = float(
                    item.get("quoteVolume", item.get("quoteAssetVolume", 0))
                )
                if quote_volume > 0:
                    pairs.append((symbol, quote_volume))
            
            except (ValueError, TypeError, KeyError):
                continue
        
        # Сортируем по волюму и берём ТОП
        pairs.sort(key=lambda x: x[1], reverse=True)
        
        logger.info(f"Найдено {len(pairs)} USDT пар, берём TOP-{limit}")
        return [pair[0] for pair in pairs[:limit]]

    async def get_all_usdt_tickers(self) -> List[Dict]:
        """Все пригодные USDT-пары без ограничения TOP."""
        data = await self._request("GET", "/api/v3/ticker/24hr", {}, weight=40)
        if not isinstance(data, list):
            return []
        active_symbols = await self.get_spot_symbol_map()
        excluded = {
            "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDP", "USDD",
            "USD1", "XUSD", "RLUSD", "EURI", "EUR", "AEUR", "PAXG", "XAUT",
        }
        result = []
        for item in data:
            try:
                symbol = item.get("symbol", "")
                if (not symbol.endswith("USDT") or symbol[:-4] in excluded or
                        symbol in settings.excluded_symbols or
                        (active_symbols and symbol not in active_symbols)):
                    continue
                result.append({
                    "symbol": symbol,
                    "price": float(item.get("lastPrice", 0)),
                    "quote_volume": float(item.get("quoteVolume", 0)),
                    "price_change_percent": float(item.get("priceChangePercent", 0)),
                    "bid": float(item.get("bidPrice", 0)),
                    "ask": float(item.get("askPrice", 0)),
                    "trade_count": int(item.get("count", 0)),
                })
            except (TypeError, ValueError):
                continue
        return result
    
    async def batch_get_tickers(self, symbols: List[str]) -> Dict[str, Dict]:
        """Получить данные для нескольких пар сразу."""
        # Binance не поддерживает batch GET для /ticker/24hr, поэтому делаем параллельные запросы с ограничением
        results = {}
        semaphore = asyncio.Semaphore(5)  # Максимум 5 одновременных запросов
        
        async def fetch_ticker(symbol):
            async with semaphore:
                return symbol, await self.get_ticker(symbol)
        
        tasks = [fetch_ticker(symbol) for symbol in symbols]
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, Exception):
                logger.warning("Ошибка пакетного ticker-запроса: %s", result)
                continue
            symbol, ticker = result
            if isinstance(ticker, dict):
                results[symbol] = ticker
        
        return results
