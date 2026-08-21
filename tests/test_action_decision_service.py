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

    def test_missing_news_fails_closed(self):
        result = ActionDecisionService.decide(
            90, NewsAssessment(available=False)
        )
        self.assertEqual(result.action, "WAIT")

    def test_news_parser_detects_hack(self):
        result = NewsSentimentService._score_posts([
            {
                "title": "Protocol exploit: withdrawals suspended",
                "votes": {"negative": 3, "important": 2},
            }
        ])
        self.assertTrue(result.critical_risk)
        self.assertLessEqual(result.score, -40)


if __name__ == "__main__":
    unittest.main()
