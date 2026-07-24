class FinalStrategyValidator:

    def __init__(self):
        pass

    def validate(self, quality, strategy_check, multi_check):

        if strategy_check["approved"] and multi_check["approved"]:
            min_quality_score = 65

        elif strategy_check["approved"]:
            min_quality_score = 75

        else:
            min_quality_score = 85

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