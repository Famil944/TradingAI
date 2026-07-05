from database.db import Database


class TradeRepository:

    def __init__(self):
        self.db = Database()
        self._init_table()

    def _init_table(self):
        with self.db.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    side TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    profit REAL,
                    close_reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()

    def save_trade(self, trade: dict):
        with self.db.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO paper_trades (
                    symbol, side, entry_price, exit_price, profit, close_reason
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                trade.get("symbol"),
                trade.get("side"),
                trade.get("entry_price"),
                trade.get("exit_price"),
                trade.get("profit"),
                trade.get("close_reason"),
            ))

            conn.commit()