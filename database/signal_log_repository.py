from database.db import Database


class SignalLogRepository:

    def __init__(self):
        self.db = Database()
        self._init_table()

    def _init_table(self):
        with self.db.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signal_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    signal TEXT,
                    price REAL,
                    score REAL,
                    quality_score REAL,
                    quality_rating TEXT,
                    strategy_approved INTEGER,
                    multi_tf_approved INTEGER,
                    final_approved INTEGER,
                    reject_reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()

    def save(self, data: dict):
        with self.db.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO signal_logs (
                    symbol,
                    signal,
                    price,
                    score,
                    quality_score,
                    quality_rating,
                    strategy_approved,
                    multi_tf_approved,
                    final_approved,
                    reject_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get("symbol"),
                data.get("signal"),
                data.get("price"),
                data.get("score"),
                data.get("quality_score"),
                data.get("quality_rating"),
                int(data.get("strategy_approved", False)),
                int(data.get("multi_tf_approved", False)),
                int(data.get("final_approved", False)),
                data.get("reject_reason"),
            ))

            conn.commit()