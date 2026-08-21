import asyncio

from config.settings import settings
from services.news_sentiment_service import NewsSentimentService


async def main():
    if not settings.cryptopanic_auth_token:
        raise SystemExit("NEWS_TOKEN_MISSING")
    result = await NewsSentimentService().assess("BTCUSDT")
    if not result.available:
        raise SystemExit("NEWS_API_UNAVAILABLE")
    print(f"NEWS_API_OK items={result.relevant_items}")


if __name__ == "__main__":
    asyncio.run(main())
