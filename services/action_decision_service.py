from dataclasses import dataclass

from services.news_sentiment_service import NewsAssessment


@dataclass(frozen=True)
class ActionDecision:
    action: str
    priority: int
    news_label: str


class ActionDecisionService:
    """Combines a technical signal with a hidden news risk assessment."""

    @staticmethod
    def decide(technical_score: int, news: NewsAssessment) -> ActionDecision:
        if not news.available:
            if technical_score >= 75:
                return ActionDecision(
                    "BUY", technical_score, "новости недоступны · технический сигнал"
                )
            return ActionDecision("WAIT", technical_score, "новости недоступны")
        priority = max(0, min(100, technical_score + news.score // 5))
        if news.critical_risk or news.score <= -40:
            return ActionDecision("AVOID", priority, "критический риск")
        if technical_score >= 75 and news.score >= -10:
            label = "положительный" if news.score >= 20 else "нейтральный"
            return ActionDecision("BUY", priority, label)
        if 65 <= technical_score < 75 and news.score >= 20:
            return ActionDecision("EARLY_BUY", priority, "положительный")
        return ActionDecision("WAIT", priority, "нейтральный")
