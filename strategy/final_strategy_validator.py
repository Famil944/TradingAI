class FinalStrategyValidator:

    def __init__(self):
        pass

    def validate(self, quality, strategy_check, multi_check):

        min_quality_score = 60

        reasons = []

        if not strategy_check["approved"]:
            reasons.append("Strategy Filter не подтвердил вход")

        if not multi_check["approved"]:
            reasons.append("Multi-Timeframe не подтвердил вход")

        if quality["score"] < min_quality_score:
            reasons.append(
                f"Quality Score ниже минимума: {quality['score']} < {min_quality_score}"
            )

        return {
            "approved": len(reasons) == 0,
            "reasons": reasons,
            "minimum_score": min_quality_score
        }
