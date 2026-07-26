class CandidateSelector:

    def select_best(
        self,
        results: list,
        excluded_symbols=None,
    ):
        candidates = self.select_candidates(results, excluded_symbols)
        return candidates[0] if candidates else None

    def select_candidates(self, results: list, excluded_symbols=None):
        excluded_symbols = set(excluded_symbols or [])
        valid = [
            item for item in (results or [])
            if item.get("symbol") not in excluded_symbols
            and item.get("signal") in {"🟢 LONG", "🔴 SHORT"}
        ]
        valid.sort(
            key=lambda item: abs(float(item.get("score", 0))),
            reverse=True,
        )
        return valid
