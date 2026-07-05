import sqlite3
from pathlib import Path


class Database:
    def __init__(self):
        self.db_path = Path("database/trading_ai.db")
        self.db_path.parent.mkdir(exist_ok=True)

    def connect(self):
        return sqlite3.connect(self.db_path)

    def init_db(self):
        with self.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    signal TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    risk TEXT NOT NULL,
                    price REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()

    def save_signal(self, data: dict):
        with self.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO signals (
                    symbol,
                    interval,
                    signal,
                    score,
                    risk,
                    price
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                data["symbol"],
                data["interval"],
                data["signal"],
                data["score"],
                data["risk"],
                data["price"],
            ))

            conn.commit()