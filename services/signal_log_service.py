from database.signal_log_repository import SignalLogRepository


class SignalLogService:

    def __init__(self):
        self.repository = SignalLogRepository()

    def save_signal_check(
        self,
        best,
        quality,
        strategy_check,
        multi_check,
        final_check
    ):
        reason = None

        if not final_check["approved"]:
            reason = "; ".join(final_check["reasons"])

        data = {
            "symbol": best.get("symbol"),
            "signal": best.get("signal"),
            "price": best.get("price"),
            "score": best.get("score"),
            "quality_score": quality.get("score"),
            "quality_rating": quality.get("rating"),
            "strategy_approved": strategy_check.get("approved"),
            "multi_tf_approved": multi_check.get("approved"),
            "final_approved": final_check.get("approved"),
            "reject_reason": reason,
        }

        return self.repository.save(data)