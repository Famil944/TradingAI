import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp

from config.settings import settings


logger = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class NewsAssessment:
    available: bool
    score: int = 0
    critical_risk: bool = False
    relevant_items: int = 0


class NewsSentimentService:
    """Produces a compact risk score; article content never reaches Telegram."""

    def __init__(self):
        self.token = settings.cryptopanic_auth_token
        self.plan = settings.cryptopanic_api_plan.strip() or "developer"
        self._cache = {}

    async def assess(self, symbol: str) -> NewsAssessment:
        ticker = symbol.upper().removesuffix("USDT")
        cached = self._cache.get(ticker)
        now = datetime.now(timezone.utc)
        if cached and cached[0] > now:
            return cached[1]
        if not self.token:
            return NewsAssessment(available=False)
        assessment = await self._fetch(ticker)
        expires = now + timedelta(minutes=max(1, settings.news_cache_minutes))
        self._cache[ticker] = (expires, assessment)
        return assessment

    async def _fetch(self, ticker: str) -> NewsAssessment:
        urls = [
            f"https://cryptopanic.com/api/{self.plan}/v2/posts/",
            "https://cryptopanic.com/api/v1/posts/",
        ]
        params = {
            "auth_token": self.token,
            "currencies": ticker,
            "kind": "news",
            "public": "true",
            "size": 30,
        }
        try:
            async with aiohttp.ClientSession() as session:
                payload = None
                last_status = None
                for url in urls:
                    async with session.get(
                        url,
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as response:
                        last_status = response.status
                        if response.status == 200:
                            payload = await response.json()
                            break
                        if response.status not in {404, 410}:
                            break
                if payload is None:
                    logger.warning("News API unavailable: HTTP %s", last_status)
                    return NewsAssessment(available=False)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            logger.exception("News API request failed")
            return NewsAssessment(available=False)
        posts = payload.get("results", []) if isinstance(payload, dict) else []
        return self._score_posts(posts)

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
                    created = datetime.fromisoformat(
                        str(published).replace("Z", "+00:00")
                    )
                    if created < cutoff:
                        continue
                except ValueError:
                    pass
            relevant += 1
            text = " ".join(
                str(post.get(key, "")) for key in ("title", "description")
            ).lower()
            normalized = re.sub(r"\s+", " ", text)
            if any(term in normalized for term in CRITICAL_TERMS):
                total -= 60
                critical = True
            total -= 15 * sum(term in normalized for term in NEGATIVE_TERMS)
            total += 10 * sum(term in normalized for term in POSITIVE_TERMS)
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
