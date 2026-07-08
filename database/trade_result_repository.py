from database.db import Database


class TradeResultRepository:

    def __init__(self):
        self.db = Database()
        self._init_table()

    def _init_table(self):
        with self.db.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    side TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    profit REAL,
                    profit_percent REAL,
                    close_reason TEXT,
                    hold_minutes REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()

    def save(self, data: dict):
        with self.db.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO trade_results (
                    symbol,
                    side,
                    entry_price,
                    exit_price,
                    profit,
                    profit_percent,
                    close_reason,
                    hold_minutes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get("symbol"),
                data.get("side"),
                data.get("entry_price"),
                data.get("exit_price"),
                data.get("profit"),
                data.get("profit_percent"),
                data.get("close_reason"),
                data.get("hold_minutes"),
            ))

            conn.commit()