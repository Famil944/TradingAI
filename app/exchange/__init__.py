"""Binance SPOT API client with rate limiting."""

import asyncio
import httpx
import logging
from typing import Optional, Any
from datetime import datetime, timedelta
from app.models import CandleData

logger = logging.getLogger(__name__)


class RateLimiter:
    """Rate limiter for Binance API (1200 weight per minute)."""
    
    def __init__(self, max_weight: int = 1200, window_minutes: int = 1):
        self.max_weight = max_weight
        self.window_minutes = window_minutes
        self.requests: list[tuple[datetime, int]] = []
    
    async def acquire(self, weight: int):
        """Wait if necessary to stay within rate limit."""
        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=self.window_minutes)
        
        # Remove old requests outside window
        self.requests = [(ts, w) for ts, w in self.requests if ts > cutoff]
        
        current_weight = sum(w for _, w in self.requests)
        
        if current_weight + weight > self.max_weight:
            # Calculate wait time
            oldest = self.requests[0][0] if self.requests else now
            wait_time = (oldest - cutoff).total_seconds() + 1
            logger.warning(f"Rate limit reached. Waiting {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
            self.requests.clear()
        
        self.requests.append((now, weight))


class BinanceClient:
    """Binance SPOT API client."""
    
    def __init__(self, base_url: str = "https://api.binance.com"):
        self.base_url = base_url
        self.rate_limiter = RateLimiter()
        self.client = httpx.AsyncClient(timeout=10)
    
    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()
    
    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        end_time: Optional[int] = None
    ) -> list[CandleData]:
        """Fetch candlestick data."""
        await self.rate_limiter.acquire(1)  # Weight: 1
        
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": min(limit, 1000)
        }
        if end_time:
            params["endTime"] = end_time
        
        url = f"{self.base_url}/api/v3/klines"
        response = await self.client.get(url, params=params)
        response.raise_for_status()
        
        data = response.json()
        candles = [
            CandleData(
                open_time=int(candle[0]),
                open_price=float(candle[1]),
                high_price=float(candle[2]),
                low_price=float(candle[3]),
                close_price=float(candle[4]),
                volume=float(candle[5]),
                close_time=int(candle[6]),
                quote_asset_volume=float(candle[7]),
                number_of_trades=int(candle[8]),
                taker_buy_base_volume=float(candle[9]),
                taker_buy_quote_volume=float(candle[10])
            )
            for candle in data
        ]
        return candles
    
    async def get_exchange_info(self) -> dict[str, Any]:
        """Get exchange information including trading pairs."""
        await self.rate_limiter.acquire(10)  # Weight: 10
        
        url = f"{self.base_url}/api/v3/exchangeInfo"
        response = await self.client.get(url)
        response.raise_for_status()
        
        return response.json()
    
    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        """Get 24h ticker data."""
        await self.rate_limiter.acquire(1)  # Weight: 1
        
        url = f"{self.base_url}/api/v3/ticker/24hr"
        response = await self.client.get(url, params={"symbol": symbol})
        response.raise_for_status()
        
        return response.json()
    
    async def get_all_tickers(self) -> list[dict[str, Any]]:
        """Get all 24h tickers."""
        await self.rate_limiter.acquire(40)  # Weight: 40
        
        url = f"{self.base_url}/api/v3/ticker/24hr"
        response = await self.client.get(url)
        response.raise_for_status()
        
        return response.json()
    
    async def get_top_symbols_by_volume(self, quote_asset: str = "USDT", limit: int = 100) -> list[str]:
        """Get top trading pairs by volume."""
        tickers = await self.get_all_tickers()
        
        # Filter by quote asset and sort by volume
        usdt_pairs = [
            (t["symbol"], float(t["quoteAssetVolume"]))
            for t in tickers
            if t["symbol"].endswith(quote_asset)
        ]
        
        # Sort by volume descending and get top pairs
        usdt_pairs.sort(key=lambda x: x[1], reverse=True)
        return [symbol for symbol, _ in usdt_pairs[:limit]]
    
    async def get_current_price(self, symbol: str) -> float:
        """Get current price for a symbol."""
        ticker = await self.get_ticker(symbol)
        return float(ticker["lastPrice"])
