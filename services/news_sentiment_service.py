import asyncio
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import aiohttp

from config.settings import settings


logger = logging.getLogger(__name__)
RSS_FEEDS = (
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
)
CRITICAL_TERMS = {
    "hack", "hacked", "exploit", "breach", "bankrupt", "insolvency",
    "delist", "delisting", "withdrawals suspended", "network halted",
    "funds frozen", "rug pull",
}
NEGATIVE_TERMS = {
    "lawsuit", "investigation", "ban", "outage", "attack", "scam",
    "unlock", "dump", "liquidation", "vulnerability",
}
POSITIVE_TERMS = {
    "approval", "approved", "partnership", "integration", "upgrade",
    "launch", "adoption", "listing", "mainnet", "institutional",
}
TICKER_ALIASES = {
    "BTC": ("bitcoin",), "ETH": ("ethereum",), "SOL": ("solana",),
    "XRP": ("ripple",), "DOGE": ("dogecoin",), "ADA": ("cardano",),
    "BNB": ("bnb", "binance coin"), "LINK": ("chainlink",),
    "AVAX": ("avalanche",), "TRX": ("tron",), "LTC": ("litecoin",),
    "BCH": ("bitcoin cash",), "DOT": ("polkadot",), "UNI": ("uniswap",),
    "HBAR": ("hedera",), "TAO": ("bittensor",), "XLM": ("stellar",),
    "TON": ("toncoin",), "NEAR": ("near protocol",), "SUI": ("sui",),
    "OP": ("optimism",), "ARB": ("arbitrum",), "INJ": ("injective",),
    "FIL": ("filecoin",), "ICP": ("internet computer",), "CRV": ("curve",),
    "STX": ("stacks",), "WLD": ("worldcoin",), "ENA": ("ethena",),
}


@dataclass(frozen=True)
class NewsAssessment:
    available: bool
    score: int = 0
    critical_risk: bool = False
    relevant_items: int = 0


class NewsSentimentService:
    """Hidden RSS risk filter; article text is never sent to Telegram."""

    def __init__(self):
        self._assessment_cache = {}
        self._feed_cache = None
        self._feed_lock = asyncio.Lock()

    async def assess(self, symbol: str) -> NewsAssessment:
        if not settings.rss_news_enabled:
            return NewsAssessment(available=False)
        ticker = symbol.upper().removesuffix("USDT")
        now = datetime.now(timezone.utc)
        cached = self._assessment_cache.get(ticker)
        if cached and cached[0] > now:
            return cached[1]
        posts, available = await self._get_posts()
        assessment = (
            self._score_posts([
                post for post in posts if self._is_relevant(ticker, post)
            ])
            if available else NewsAssessment(available=False)
        )
        expires = now + timedelta(minutes=max(1, settings.news_cache_minutes))
        self._assessment_cache[ticker] = (expires, assessment)
        return assessment

    async def _get_posts(self):
        now = datetime.now(timezone.utc)
        if self._feed_cache and self._feed_cache[0] > now:
            return self._feed_cache[1], self._feed_cache[2]
        async with self._feed_lock:
            if self._feed_cache and self._feed_cache[0] > now:
                return self._feed_cache[1], self._feed_cache[2]
            posts, available = await self._fetch_feeds()
            expires = now + timedelta(minutes=max(1, settings.news_cache_minutes))
            self._feed_cache = (expires, posts, available)
            return posts, available

    async def _fetch_feeds(self):
        headers = {"User-Agent": "TradingAI/1.0 RSS risk filter"}
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            results = await asyncio.gather(
                *(self._fetch_feed(session, source, url) for source, url in RSS_FEEDS),
                return_exceptions=True,
            )
        posts = []
        available = False
        for result in results:
            if isinstance(result, Exception):
                logger.warning("RSS source unavailable: %s", type(result).__name__)
                continue
            available = True
            posts.extend(result)
        deduplicated = {}
        for post in posts:
            title = re.sub(
                r"[^a-z0-9]+", " ", (post.get("title") or "").lower()
            ).strip()
            key = title or (post.get("link") or "").strip().lower()
            if key:
                deduplicated[key] = post
        return list(deduplicated.values()), available

    @staticmethod
    async def _fetch_feed(session, source, url):
        async with session.get(url) as response:
            if response.status != 200:
                raise aiohttp.ClientResponseError(
                    response.request_info, response.history, status=response.status
                )
            return NewsSentimentService._parse_rss(await response.text(), source)

    @staticmethod
    def _parse_rss(payload: str, source: str):
        root = ElementTree.fromstring(payload)
        posts = []
        for item in root.findall(".//item"):
            def value(name):
                node = item.find(name)
                return (node.text or "").strip() if node is not None else ""
            published = value("pubDate")
            if published:
                try:
                    published = parsedate_to_datetime(published).astimezone(
                        timezone.utc
                    ).isoformat()
                except (TypeError, ValueError):
                    published = ""
            description = re.sub(r"<[^>]+>", " ", value("description"))
            posts.append({
                "title": html.unescape(value("title")),
                "description": html.unescape(description),
                "link": value("link"),
                "published_at": published,
                "source": source,
            })
        return posts

    @staticmethod
    def _is_relevant(ticker: str, post: dict) -> bool:
        text = " ".join(str(post.get(key, "")) for key in ("title", "description"))
        normalized = re.sub(r"\s+", " ", text).lower()
        aliases = TICKER_ALIASES.get(ticker, ())
        # Короткие неизвестные тикеры (AI, U, RE...) часто являются обычными
        # словами и дают ложную связь с новостью. Для них нужно известное имя.
        ticker_terms = (ticker.lower(),) if len(ticker) >= 4 or aliases else ()
        terms = (*ticker_terms, *aliases)
        return any(re.search(
            rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", normalized
        ) for term in terms)

    @staticmethod
    def _has_term(text: str, term: str) -> bool:
        return bool(re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", text))

    @staticmethod
    def _score_posts(posts) -> NewsAssessment:
        total = 0
        critical = False
        relevant = 0
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for post in posts:
            published = post.get("published_at") or post.get("created_at")
            if published:
                try:
                    created = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                    if created < cutoff:
                        continue
                except ValueError:
                    pass
            relevant += 1
            text = " ".join(str(post.get(key, "")) for key in ("title", "description"))
            normalized = re.sub(r"\s+", " ", text).lower()
            if any(NewsSentimentService._has_term(normalized, term) for term in CRITICAL_TERMS):
                total -= 60
                critical = True
            total -= 15 * sum(
                NewsSentimentService._has_term(normalized, term) for term in NEGATIVE_TERMS
            )
            total += 10 * sum(
                NewsSentimentService._has_term(normalized, term) for term in POSITIVE_TERMS
            )
            votes = post.get("votes") or {}
            total += min(20, int(votes.get("positive", 0)) * 2)
            total -= min(30, int(votes.get("negative", 0)) * 3)
            if int(votes.get("important", 0)) and total < 0:
                total -= 10
        return NewsAssessment(
            available=True,
            score=max(-100, min(100, total)),
            critical_risk=critical,
            relevant_items=relevant,
        )
