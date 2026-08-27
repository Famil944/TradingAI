from validation.filter_results import FilterResults


class StrategyValidator:

    def __init__(self):
        self.filters = FilterResults()

    def validate(self, strategy_check, multi_check, quality):

        if strategy_check["approved"] and multi_check["approved"]:
            minimum_score = 65

        elif strategy_check["approved"]:
            minimum_score = 75

        else:
            minimum_score = 85

        self.filters.add(
            "Strategy Filter",
            strategy_check["approved"]
        )

        self.filters.add(
            "Multi TF",
            multi_check["approved"]
        )

        self.filters.add(
            f"Quality Score >= {minimum_score}",
            quality["score"] >= minimum_score
        )

    def report(self):
        return self.filters.summary()