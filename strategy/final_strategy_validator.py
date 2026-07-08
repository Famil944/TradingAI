class FinalStrategyValidator:

    def __init__(self, min_quality_score=70):
        self.min_quality_score = min_quality_score

    def validate(self, quality, strategy_check, multi_check):
        reasons = []

        if not strategy_check["approved"]:
            reasons.append("Strategy Filter не подтвердил вход")

        if not multi_check["approved"]:
            reasons.append("Multi-Timeframe не подтвердил вход")

        if quality["score"] < self.min_quality_score:
            reasons.append(
                f"Quality Score ниже минимума: {quality['score']} < {self.min_quality_score}"
            )

        return {
            "approved": len(reasons) == 0,
            "reasons": reasons
        }