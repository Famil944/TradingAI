import sqlite3
import os
from pathlib import Path


class ManagedConnection(sqlite3.Connection):
    """A sqlite connection that is also closed by a with statement."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Database:
    def __init__(self):
        project_root = Path(__file__).resolve().parent.parent
        configured_path = os.getenv("TRADING_AI_DB_PATH")
        self.db_path = (
            Path(configured_path).expanduser().resolve()
            if configured_path
            else project_root / "database" / "trading_ai.db"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self):
        connection = sqlite3.connect(
            self.db_path,
            timeout=10,
            factory=ManagedConnection,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

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
