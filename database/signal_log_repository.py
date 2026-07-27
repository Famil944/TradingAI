from database.db import Database


class SignalLogRepository:

    def __init__(self):
        self.db = Database()
        self._init_table()
        self._add_missing_columns()

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

    def _add_missing_columns(self):
        with self.db.connect() as conn:
            existing = {
                column[1]
                for column in conn.execute(
                    "PRAGMA table_info(signal_logs)"
                ).fetchall()
            }
            columns = {
                "strategy_reason": "TEXT",
                "multi_tf_match_count": "INTEGER",
                "multi_tf_required": "INTEGER",
                "multi_tf_avg_score": "REAL",
                "execution_status": "TEXT",
                "execution_error": "TEXT",
            }
            for name, column_type in columns.items():
                if name not in existing:
                    conn.execute(
                        f"ALTER TABLE signal_logs "
                        f"ADD COLUMN {name} {column_type}"
                    )

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
                    reject_reason,
                    strategy_reason,
                    multi_tf_match_count,
                    multi_tf_required,
                    multi_tf_avg_score,
                    execution_status,
                    execution_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                data.get("strategy_reason"),
                data.get("multi_tf_match_count"),
                data.get("multi_tf_required"),
                data.get("multi_tf_avg_score"),
                data.get("execution_status"),
                data.get("execution_error"),
            ))

            conn.commit()
