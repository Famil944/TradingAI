from validation.filter_results import FilterResults


class StrategyValidator:

    def __init__(self):
        self.filters = FilterResults()

    def validate(self, strategy_check, multi_check, quality):
        self.filters.add(
            "Strategy Filter",
            strategy_check["approved"]
        )

        self.filters.add(
            "Multi TF",
            multi_check["approved"]
        )

        self.filters.add(
            "Quality Score >= 70",
            quality["score"] >= 70
        )

    def report(self):
        return self.filters.summary()