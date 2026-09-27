from dataclasses import dataclass

from config.settings import settings


@dataclass(frozen=True)
class RiskScore:
    score: int
    level: str
    reasons: tuple[str, ...]


class RiskScoringService:
    @staticmethod
    def calculate(
        *, listing_days, quote_volume, spread_percent, trend_metrics,
        drawdown_30d, market_regime,
    ) -> RiskScore:
        score = 0
        reasons = []
        if listing_days < 180:
            score += 15
            reasons.append("короткая история торгов")
        elif listing_days < 365:
            score += 7
            reasons.append("история торгов менее года")
        if trend_metrics:
            if ((trend_metrics.get("daily_ema20") or 0)
                    < (trend_metrics.get("daily_ema50") or 0)):
                score += 15
                reasons.append("дневная EMA20 ниже EMA50")
            if trend_metrics.get("listing_decline_percent", 0) >= 20:
                score += 10
                reasons.append("длительное снижение цены")
            if trend_metrics.get("distance_from_history_low_percent", 999) <= 10:
                score += 10
                reasons.append("цена около минимума истории")
        if drawdown_30d >= 25:
            score += 10
            reasons.append("очень глубокая просадка за 30 дней")
        if quote_volume < settings.min_quote_volume_usdt * 2:
            score += 10
            reasons.append("ликвидность близка к минимуму")
        if spread_percent is not None and spread_percent > settings.max_spread_percent / 2:
            score += 10
            reasons.append("повышенный спред")
        if market_regime.name == "CAUTION":
            score += 10
            reasons.append("рынок BTC в режиме осторожности")
        elif market_regime.name == "HIGH_RISK":
            score += 25
            reasons.append("рынок BTC в режиме высокого риска")
        score = min(100, score)
        level = "LOW" if score < 30 else "MEDIUM" if score < 55 else "HIGH"
        return RiskScore(score, level, tuple(reasons))
