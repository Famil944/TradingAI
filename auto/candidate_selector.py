class CandidateSelector:

    def select_best(self, results: list):
        if not results:
            return None

        valid = []

        for item in results:
            signal = item.get("signal")

            if signal in ["🟢 LONG", "🔴 SHORT"]:
                valid.append(item)

        if not valid:
            return None

        valid.sort(
            key=lambda x: abs(x.get("score", 0)),
            reverse=True
        )

        return valid[0]