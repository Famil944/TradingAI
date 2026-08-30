import aiohttp
import asyncio
from typing import List, Dict, Optional
from datetime import datetime
from config.models import CandleData
from config.settings import settings
import logging

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
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self._endpoint_probe_lock = asyncio.Lock()
    
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
        """Выполнить запрос; endpoint с HTTP 451 повторно не опрашивается."""
        await self.RATE_LIMITER.acquire(weight)

        preferred = type(self)._preferred_base_url
        bases = list(dict.fromkeys((preferred, self.BASE_URL, *self.PUBLIC_BASE_URLS)))
        for base_url in (base for base in bases if base):
            if base_url in type(self)._blocked_base_urls:
                continue
            if base_url != type(self)._preferred_base_url:
                async with self._endpoint_probe_lock:
                    if base_url in type(self)._blocked_base_urls:
                        continue
                    if (type(self)._preferred_base_url and
                            base_url != type(self)._preferred_base_url):
                        continue
                    status, data = await self._request_from_base(
                        method, base_url, endpoint, params
                    )
            else:
                status, data = await self._request_from_base(
                    method, base_url, endpoint, params
                )
            if status == "success":
                if type(self)._preferred_base_url != base_url:
                    type(self)._preferred_base_url = base_url
                    if base_url != self.BASE_URL:
                        logger.info("Binance fallback endpoint активен: %s", base_url)
                return data
            if status == "terminal":
                return None
        logger.error("Все публичные Binance Spot endpoints недоступны для %s", endpoint)
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
                volume=float(candle[7])
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
        data = await self._request(
            "GET", "/api/v3/exchangeInfo", {"symbol": symbol}, weight=1
        )
        symbols = data.get("symbols", []) if isinstance(data, dict) else []
        if not symbols:
            return None
        for item in symbols[0].get("filters", []):
            if item.get("filterType") == "PRICE_FILTER":
                try:
                    tick_size = float(item["tickSize"])
                    return tick_size if tick_size > 0 else None
                except (KeyError, TypeError, ValueError):
                    return None
        return None
    
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
        
        # Эти активы фактически дублируют доллар/фиат и занимают места в TOP,
        # но не подходят для стратегии роста к цели +3%.
        excluded_quote_like_assets = {
            "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDP", "USDD",
            "USD1", "XUSD", "RLUSD", "EURI", "EUR", "AEUR", "PAXG",
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
