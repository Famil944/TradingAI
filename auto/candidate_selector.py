class CandidateSelector:

    def select_best(
        self,
        results: list,
        excluded_symbols=None,
    ):
        if not results:
            return None

        excluded_symbols = set(excluded_symbols or [])

        valid = []

        for item in results:
            symbol = item.get("symbol")
            signal = item.get("signal")

            if symbol in excluded_symbols:
                continue

            if signal in ["🟢 LONG", "🔴 SHORT"]:
                valid.append(item)

        if not valid:
            return None

        valid.sort(
            key=lambda item: float(item.get("score", 0)),
            reverse=True,
        )

        return valid[0]