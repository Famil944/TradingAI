import asyncio

from config.settings import settings
from services.news_sentiment_service import NewsSentimentService


async def main():
    if not settings.rss_news_enabled:
        raise SystemExit("RSS_NEWS_DISABLED")
    result = await NewsSentimentService().assess("BTCUSDT")
    if not result.available:
        raise SystemExit("RSS_NEWS_UNAVAILABLE")
    print(f"RSS_NEWS_OK items={result.relevant_items} score={result.score}")


if __name__ == "__main__":
    asyncio.run(main())
