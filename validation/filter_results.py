class FilterResults:

    def __init__(self):
        self.results = {}

    def add(self, filter_name, passed):
        if filter_name not in self.results:
            self.results[filter_name] = {
                "pass": 0,
                "fail": 0
            }

        if passed:
            self.results[filter_name]["pass"] += 1
        else:
            self.results[filter_name]["fail"] += 1

    def summary(self):
        return self.results