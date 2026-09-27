import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp

from config.settings import settings


logger = logging.getLogger(__name__)
PRODUCTS_URL = (
    "https://www.binance.com/bapi/asset/v2/public/asset-service/"
    "product/get-products?includeEtf=true"
)
DELISTING_URL = (
    "https://www.binance.com/bapi/composite/v1/public/cms/article/"
    "catalog/list/query?catalogId=161&pageNo=1&pageSize=30"
)
ARTICLE_URL = (
    "https://www.binance.com/bapi/composite/v1/public/cms/article/"
    "detail/query?articleCode={code}"
)


@dataclass(frozen=True)
class BinanceRiskAssessment:
    available: bool
    blocked: bool = False
    risk_code: str = "NONE"
    tags: tuple[str, ...] = ()
    status: str = "UNKNOWN"


class BinanceRiskService:
    """Кэш официальных Spot-тегов Binance без торговых API-ключей."""

    def __init__(self):
        self._products = None
        self._expires_at = None
        self._risk_events = {}
        self._lock = asyncio.Lock()
        self.last_success_at = None
        self.last_error = None

    async def refresh(self, force=False) -> bool:
        now = datetime.now(timezone.utc)
        if (not force and self._products is not None and self._expires_at
                and self._expires_at > now):
            return True
        async with self._lock:
            now = datetime.now(timezone.utc)
            if (not force and self._products is not None and self._expires_at
                    and self._expires_at > now):
                return True
            try:
                timeout = aiohttp.ClientTimeout(
                    total=settings.binance_risk_timeout_seconds
                )
                headers = {"User-Agent": "TradingAI/1.0 risk-filter"}
                async with aiohttp.ClientSession(
                    timeout=timeout, headers=headers
                ) as session:
                    products_payload, announcements_payload = await asyncio.gather(
                        self._fetch_json(session, PRODUCTS_URL),
                        self._fetch_json(session, DELISTING_URL),
                    )
                    events = await self._parse_risk_events(
                        session, announcements_payload
                    )
                products = self._parse_products(products_payload)
                if not products:
                    raise ValueError("official Binance product catalog is empty")
                self._products = products
                self._risk_events = events
                self._expires_at = now + timedelta(
                    minutes=max(5, settings.binance_risk_cache_minutes)
                )
                self.last_success_at = now.isoformat()
                self.last_error = None
                return True
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                self.last_error = f"{type(error).__name__}: {error}"
                logger.warning("Official Binance risk catalog unavailable: %s", error)
                # Просроченный успешный кэш безопаснее полного отсутствия данных.
                return self._products is not None

    @staticmethod
    async def _fetch_json(session, url):
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json()

    @staticmethod
    def _parse_products(payload):
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return {}
        result = {}
        for row in rows:
            symbol = str(row.get("s") or "").upper()
            if not symbol:
                continue
            result[symbol] = {
                "status": str(row.get("st") or "UNKNOWN").upper(),
                "tags": tuple(str(tag) for tag in (row.get("tags") or [])),
            }
        return result

    @staticmethod
    def _body_text(body):
        try:
            body = json.loads(body) if isinstance(body, str) else body
        except json.JSONDecodeError:
            return str(body or "")
        parts = []

        def walk(value):
            if isinstance(value, dict):
                if isinstance(value.get("text"), str):
                    parts.append(value["text"])
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(body)
        return " ".join(parts)

    @classmethod
    async def _parse_risk_events(cls, session, payload):
        rows = ((payload.get("data") or {}).get("articles")
                if isinstance(payload, dict) else None) or []
        relevant = []
        events = {}
        for row in rows:
            title = str(row.get("title") or "")
            if "Margin Trading Pairs" in title:
                continue
            match = re.search(
                r"Binance Will Delist (.+?)(?: on | from |$)", title,
                flags=re.IGNORECASE,
            )
            if match:
                for ticker in re.findall(r"\b[A-Z0-9]{2,15}\b", match.group(1)):
                    events[f"{ticker}USDT"] = "DELISTING"
            if "Removal of Spot Trading Pairs" in title and row.get("code"):
                relevant.append(row["code"])
        details = await asyncio.gather(*(
            cls._fetch_json(session, ARTICLE_URL.format(code=code))
            for code in relevant[:10]
        ))
        for detail in details:
            body = ((detail.get("data") or {}).get("body")
                    if isinstance(detail, dict) else "")
            text = cls._body_text(body)
            for ticker in re.findall(r"\b([A-Z0-9]{2,20})/USDT\b", text):
                events[f"{ticker}USDT"] = "SPOT_PAIR_REMOVAL"
        return events

    async def assess(self, symbol: str) -> BinanceRiskAssessment:
        if not await self.refresh():
            return BinanceRiskAssessment(available=False)
        event = self._risk_events.get(symbol.upper())
        item = (self._products or {}).get(symbol.upper())
        if event:
            return BinanceRiskAssessment(
                available=True, blocked=True, risk_code=event,
                tags=item["tags"] if item else (),
                status=item["status"] if item else "DELISTING",
            )
        if not item:
            return BinanceRiskAssessment(
                available=False, risk_code="SYMBOL_METADATA_UNAVAILABLE"
            )
        tags = item["tags"]
        normalized_tags = {tag.casefold() for tag in tags}
        status = item["status"]
        if status != "TRADING":
            return BinanceRiskAssessment(
                available=True, blocked=True, risk_code="TRADING_SUSPENSION",
                tags=tags, status=status,
            )
        if "monitoring" in normalized_tags:
            return BinanceRiskAssessment(
                available=True, blocked=True, risk_code="MONITORING_TAG",
                tags=tags, status=status,
            )
        return BinanceRiskAssessment(
            available=True, tags=tags, status=status,
        )

    def diagnostics(self):
        return {
            "available": self._products is not None,
            "cached_symbols": len(self._products or {}),
            "active_risk_events": len(self._risk_events),
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
        }
