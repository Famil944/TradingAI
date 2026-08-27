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
    def __init__(self, db_path: str = None):
        configured_path = db_path or os.getenv("TRADING_AI_DB_PATH")
        if configured_path:
            self.db_path = Path(configured_path).expanduser().resolve()
        else:
            project_root = Path(__file__).resolve().parent.parent
            self.db_path = project_root / "data" / "bot.db"
        
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self):
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=10,
            factory=ManagedConnection,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def init_db(self):
        """Инициализация всех таблиц БД."""
        with self.connect() as conn:
            cursor = conn.cursor()

            # Таблица сигналов
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    entry_zone_min REAL NOT NULL,
                    entry_zone_max REAL NOT NULL,
                    tp1 REAL NOT NULL,
                    tp2 REAL NOT NULL,
                    tp3 REAL NOT NULL,
                    tp4 REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    stop_loss_percent REAL NOT NULL,
                    support REAL NOT NULL,
                    resistance REAL NOT NULL,
                    rsi_5m REAL,
                    rsi_15m REAL,
                    rsi_1h REAL,
                    volume_change_percent REAL,
                    risk_reward REAL NOT NULL,
                    reasons TEXT,
                    warnings TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    sent_to_telegram INTEGER DEFAULT 0
                )
            """)

            # Таблица отслеживания сигналов
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signal_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    max_price REAL NOT NULL,
                    max_favorable_excursion REAL NOT NULL,
                    tp1_hit INTEGER DEFAULT 0,
                    tp2_hit INTEGER DEFAULT 0,
                    tp3_hit INTEGER DEFAULT 0,
                    tp4_hit INTEGER DEFAULT 0,
                    stop_hit INTEGER DEFAULT 0,
                    final_result TEXT,
                    closed_at TIMESTAMP,
                    FOREIGN KEY (signal_id) REFERENCES signals(id)
                )
            """)

            # Таблица пользовательских настроек
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    min_score INTEGER DEFAULT 75,
                    min_target_percent INTEGER DEFAULT 1,
                    timeframes TEXT DEFAULT '5m,15m,1h,4h',
                    signal_count INTEGER DEFAULT 10,
                    auto_notifications INTEGER DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            columns = {
                row[1] for row in cursor.execute("PRAGMA table_info(user_settings)")
            }
            if "scan_profile" not in columns:
                cursor.execute(
                    "ALTER TABLE user_settings ADD COLUMN scan_profile TEXT DEFAULT 'more'"
                )

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS watched_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    signal_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    entry_zone_min REAL NOT NULL,
                    entry_zone_max REAL NOT NULL,
                    tp1 REAL NOT NULL,
                    tp2 REAL NOT NULL,
                    tp3 REAL NOT NULL,
                    tp4 REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    entered INTEGER DEFAULT 0,
                    tp1_hit INTEGER DEFAULT 0,
                    tp2_hit INTEGER DEFAULT 0,
                    tp3_hit INTEGER DEFAULT 0,
                    tp4_hit INTEGER DEFAULT 0,
                    stop_hit INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, signal_id),
                    FOREIGN KEY (signal_id) REFERENCES signals(id)
                )
            """)

            # Таблица последних отправленных сигналов (для cooldown)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signal_cooldown (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL UNIQUE,
                    last_signal_id INTEGER NOT NULL,
                    last_score INTEGER NOT NULL,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (last_signal_id) REFERENCES signals(id)
                )
            """)

            # Сделки, которые пользователь действительно открыл вручную на Binance.
            # Они не имеют срока действия и переживают любые перезапуски бота.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS manual_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    signal_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    current_price REAL NOT NULL,
                    tp1 REAL NOT NULL,
                    tp2 REAL NOT NULL,
                    tp3 REAL NOT NULL,
                    tp4 REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    max_price REAL NOT NULL,
                    min_price REAL NOT NULL,
                    tp1_hit INTEGER DEFAULT 0,
                    tp2_hit INTEGER DEFAULT 0,
                    tp3_hit INTEGER DEFAULT 0,
                    tp4_hit INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'open',
                    close_price REAL,
                    close_reason TEXT,
                    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP,
                    last_checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    critical_alerted INTEGER DEFAULT 0,
                    FOREIGN KEY (signal_id) REFERENCES signals(id)
                )
            """)
            trade_columns = {
                row[1] for row in cursor.execute("PRAGMA table_info(manual_trades)")
            }
            if "current_price" not in trade_columns:
                cursor.execute(
                    "ALTER TABLE manual_trades ADD COLUMN current_price REAL"
                )
                cursor.execute(
                    "UPDATE manual_trades SET current_price = entry_price "
                    "WHERE current_price IS NULL"
                )
            if "critical_alerted" not in trade_columns:
                cursor.execute(
                    "ALTER TABLE manual_trades ADD COLUMN critical_alerted INTEGER DEFAULT 0"
                )

            conn.commit()

    def open_manual_trade(self, user_id: int, signal_id: int, entry_price: float):
        """Записать фактически выбранную пользователем сделку."""
        with self.connect() as conn:
            existing = conn.execute(
                """SELECT id FROM manual_trades
                   WHERE user_id = ? AND symbol = (
                       SELECT symbol FROM signals WHERE id = ?
                   ) AND status = 'open'""",
                (int(user_id), int(signal_id)),
            ).fetchone()
            if existing:
                return None
            signal = conn.execute(
                """SELECT symbol, score, tp1, tp2, tp3, tp4, stop_loss
                   FROM signals WHERE id = ?""",
                (int(signal_id),),
            ).fetchone()
            if not signal:
                return False
            cursor = conn.execute(
                """INSERT INTO manual_trades (
                       user_id, signal_id, symbol, score, entry_price, current_price,
                       tp1, tp2, tp3, tp4, stop_loss, max_price, min_price
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(user_id), int(signal_id), signal[0], signal[1],
                    float(entry_price), float(entry_price), signal[2], signal[3], signal[4],
                    signal[5], signal[6], float(entry_price), float(entry_price),
                ),
            )
            return cursor.lastrowid

    def get_manual_trades(self, user_id: int = None, status: str = None):
        query = "SELECT * FROM manual_trades WHERE 1 = 1"
        params = []
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(int(user_id))
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY opened_at DESC"
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def update_manual_trade(self, trade_id: int, **fields):
        allowed = {
            "max_price", "min_price", "tp1_hit", "tp2_hit", "tp3_hit",
            "tp4_hit", "status", "close_price", "close_reason",
            "closed_at", "last_checked_at", "current_price", "critical_alerted",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE manual_trades SET {assignments} WHERE id = ?",
                (*updates.values(), int(trade_id)),
            )

    def close_manual_trade(self, trade_id: int, user_id: int, price: float,
                           reason: str = "manual") -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE manual_trades
                   SET status = 'closed', close_price = ?, close_reason = ?,
                       closed_at = CURRENT_TIMESTAMP,
                       last_checked_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND user_id = ? AND status = 'open'""",
                (float(price), reason, int(trade_id), int(user_id)),
            )
            return cursor.rowcount > 0
    
    def save_signal(self, signal_data: dict) -> int:
        """Сохранить сигнал в БД. Возвращает ID сигнала."""
        with self.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO signals (
                    symbol,
                    score,
                    entry_price,
                    entry_zone_min,
                    entry_zone_max,
                    tp1,
                    tp2,
                    tp3,
                    tp4,
                    stop_loss,
                    stop_loss_percent,
                    support,
                    resistance,
                    rsi_5m,
                    rsi_15m,
                    rsi_1h,
                    volume_change_percent,
                    risk_reward,
                    reasons,
                    warnings
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal_data.get("symbol"),
                signal_data.get("score"),
                signal_data.get("entry_price"),
                signal_data.get("entry_zone_min"),
                signal_data.get("entry_zone_max"),
                signal_data.get("tp1"),
                signal_data.get("tp2"),
                signal_data.get("tp3"),
                signal_data.get("tp4"),
                signal_data.get("stop_loss"),
                signal_data.get("stop_loss_percent"),
                signal_data.get("support"),
                signal_data.get("resistance"),
                signal_data.get("rsi_5m"),
                signal_data.get("rsi_15m"),
                signal_data.get("rsi_1h"),
                signal_data.get("volume_change_percent"),
                signal_data.get("risk_reward"),
                ",".join(signal_data.get("reasons", [])),
                ",".join(signal_data.get("warnings", []))
            ))

            conn.commit()
            return cursor.lastrowid
    
    def get_signals(self, limit: int = 10) -> list:
        """Получить последние сигналы."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM signals 
                ORDER BY created_at DESC 
                LIMIT ?
            """, (limit,))
            return cursor.fetchall()

    def get_top_signals(self, limit: int = 10, hours: int = 24) -> list:
        """Лучшие свежие сигналы, по одному последнему результату на пару."""
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM signals s
                WHERE s.created_at >= datetime('now', ?)
                  AND s.id = (
                    SELECT MAX(s2.id) FROM signals s2 WHERE s2.symbol = s.symbol
                  )
                ORDER BY s.score DESC, s.created_at DESC
                LIMIT ?
                """,
                (f"-{int(hours)} hours", int(limit)),
            ).fetchall()

    def get_signal_statistics(self) -> dict:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
            row = conn.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(tp1_hit), 0), COALESCE(SUM(tp2_hit), 0),
                       COALESCE(SUM(tp3_hit), 0), COALESCE(SUM(tp4_hit), 0),
                       COALESCE(SUM(stop_hit), 0)
                FROM signal_results
                """
            ).fetchone()
        return {
            "signals": total, "tracked": row[0], "tp1": row[1],
            "tp2": row[2], "tp3": row[3], "tp4": row[4], "stops": row[5],
        }

    def register_user(self, user_id: int):
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO user_settings (user_id, auto_notifications, updated_at)
                VALUES (?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET updated_at = CURRENT_TIMESTAMP
                """,
                (int(user_id),),
            )

    def get_notification_user_ids(self) -> list[int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT user_id FROM user_settings WHERE auto_notifications = 1"
            ).fetchall()
        return [int(row[0]) for row in rows]

    def get_scan_profile(self, user_id: int) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT scan_profile FROM user_settings WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
        return row[0] if row and row[0] else "more"

    def set_scan_profile(self, user_id: int, profile: str):
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO user_settings (user_id, scan_profile, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    scan_profile = excluded.scan_profile,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (int(user_id), profile),
            )

    def watch_signal(self, user_id: int, signal_id: int, validity_minutes: int):
        with self.connect() as conn:
            signal = conn.execute(
                """
                SELECT symbol, entry_zone_min, entry_zone_max, tp1, tp2, tp3,
                       tp4, stop_loss,
                       datetime(created_at, '+' || ? || ' minutes')
                FROM signals WHERE id = ?
                """,
                (int(validity_minutes), int(signal_id)),
            ).fetchone()
            if not signal:
                return False
            conn.execute(
                """
                INSERT INTO watched_signals (
                    user_id, signal_id, symbol, entry_zone_min, entry_zone_max,
                    tp1, tp2, tp3, tp4, stop_loss, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, signal_id) DO UPDATE SET
                    status = 'active', updated_at = CURRENT_TIMESTAMP
                """,
                (int(user_id), int(signal_id), *signal),
            )
        return True

    def get_active_watches(self) -> list:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM watched_signals WHERE status = 'active'"
            ).fetchall()

    def update_watch(self, watch_id: int, **fields):
        allowed = {
            "entered", "tp1_hit", "tp2_hit", "tp3_hit", "tp4_hit",
            "stop_hit", "status",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE watched_signals SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (*updates.values(), int(watch_id)),
            )
    
    def update_cooldown(self, symbol: str, signal_id: int, score: int):
        """Обновить время cooldown для символа."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO signal_cooldown (symbol, last_signal_id, last_score)
                VALUES (?, ?, ?)
            """, (symbol, signal_id, score))
            conn.commit()
    
    def get_cooldown_signal(self, symbol: str):
        """Получить информацию о последнем сигнале для символа."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT last_signal_id, last_score, sent_at FROM signal_cooldown
                WHERE symbol = ?
            """, (symbol,))
            return cursor.fetchone()
