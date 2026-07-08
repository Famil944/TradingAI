from database.db import Database


class TradeAnalyticsService:

    def __init__(self):
        self.db = Database()

    def summary(self):
        with self.db.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    COUNT(*),
                    COALESCE(SUM(profit),0),
                    COALESCE(AVG(profit),0),
                    COALESCE(MAX(profit),0),
                    COALESCE(MIN(profit),0)
                FROM trade_results
            """)

            count, total, average, best, worst = cursor.fetchone()

            return {
                "count": count,
                "total_profit": round(total, 2),
                "average_profit": round(average, 2),
                "best_trade": round(best, 2),
                "worst_trade": round(worst, 2),
            }