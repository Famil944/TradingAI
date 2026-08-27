from database.db import Database


class HistoryRepository:
    def __init__(self):
        self.db = Database()
        self.db.init_db()

    def get_last_signals(self, limit: int = 20):
        with self.db.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    symbol,
                    signal,
                    score,
                    price,
                    created_at
                FROM signals
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))

            return cursor.fetchall()