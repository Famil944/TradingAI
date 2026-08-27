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
        final_check,
        execution_status=None,
        execution_error=None,
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
            "strategy_reason": strategy_check.get("reason"),
            "multi_tf_match_count": multi_check.get("match_count"),
            "multi_tf_required": multi_check.get("required"),
            "multi_tf_avg_score": multi_check.get("avg_score"),
            "execution_status": execution_status,
            "execution_error": execution_error,
        }

        return self.repository.save(data)
