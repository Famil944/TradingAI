import unittest

from services.action_decision_service import ActionDecisionService
from services.news_sentiment_service import NewsAssessment, NewsSentimentService


class ActionDecisionServiceTests(unittest.TestCase):
    def test_strong_technical_signal_with_neutral_news_is_buy(self):
        result = ActionDecisionService.decide(
            78, NewsAssessment(available=True, score=0)
        )
        self.assertEqual(result.action, "BUY")

    def test_early_signal_requires_positive_news(self):
        positive = ActionDecisionService.decide(
            68, NewsAssessment(available=True, score=25)
        )
        neutral = ActionDecisionService.decide(
            68, NewsAssessment(available=True, score=0)
        )
        self.assertEqual(positive.action, "EARLY_BUY")
        self.assertEqual(neutral.action, "WAIT")

    def test_critical_news_blocks_even_strong_signal(self):
        result = ActionDecisionService.decide(
            90, NewsAssessment(available=True, score=-60, critical_risk=True)
        )
        self.assertEqual(result.action, "AVOID")

    def test_missing_news_keeps_strong_technical_signal(self):
        result = ActionDecisionService.decide(
            90, NewsAssessment(available=False)
        )
        self.assertEqual(result.action, "BUY")

    def test_news_parser_detects_hack(self):
        result = NewsSentimentService._score_posts([
            {
                "title": "Protocol exploit: withdrawals suspended",
                "votes": {"negative": 3, "important": 2},
            }
        ])
        self.assertTrue(result.critical_risk)
        self.assertLessEqual(result.score, -40)

    def test_rss_parser_and_coin_relevance(self):
        rss = """<?xml version="1.0"?><rss><channel><item>
        <title>Ethereum network upgrade approved</title>
        <description>ETH adoption grows</description>
        <link>https://example.test/eth</link>
        <pubDate>Sun, 30 Aug 2026 08:00:00 +0000</pubDate>
        </item></channel></rss>"""
        posts = NewsSentimentService._parse_rss(rss, "Test")
        self.assertEqual(len(posts), 1)
        self.assertTrue(NewsSentimentService._is_relevant("ETH", posts[0]))
        self.assertFalse(NewsSentimentService._is_relevant("BTC", posts[0]))

    def test_short_ticker_does_not_match_inside_word(self):
        post = {"title": "Bank investigation continues", "description": ""}
        self.assertFalse(NewsSentimentService._is_relevant("ANK", post))
        result = NewsSentimentService._score_posts([post])
        self.assertEqual(result.score, -15)

    def test_unknown_short_ticker_does_not_match_common_word(self):
        post = {"title": "AI market adoption grows", "description": ""}
        self.assertFalse(NewsSentimentService._is_relevant("AI", post))


if __name__ == "__main__":
    unittest.main()
