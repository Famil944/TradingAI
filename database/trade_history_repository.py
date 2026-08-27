from database.db import Database


class TradeHistoryRepository:

    def __init__(self):
        self.db = Database()

    def get_last_trades(self, limit: int = 10):
        with self.db.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT symbol, side, entry_price, exit_price, profit, close_reason, created_at
                FROM paper_trades
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))

            return cursor.fetchall()