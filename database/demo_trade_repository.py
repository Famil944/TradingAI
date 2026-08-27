from database.db import Database


class DemoTradeRepository:

    def __init__(self):
        self.db = Database()
        self._init_table()
        self._add_missing_columns()

    def _init_table(self):
        with self.db.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS demo_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    side TEXT,
                    entry_price REAL,
                    quantity REAL,
                    take_profit REAL,
                    stop_loss REAL,
                    status TEXT,
                    exit_price REAL,
                    pnl REAL,
                    pnl_percent REAL,
                    close_reason TEXT,
                    closed_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()

    def _add_missing_columns(self):
        with self.db.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("PRAGMA table_info(demo_trades)")
            existing_columns = {
                column[1] for column in cursor.fetchall()
            }

            new_columns = {
                "exit_price": "REAL",
                "pnl": "REAL",
                "pnl_percent": "REAL",
                "close_reason": "TEXT",
                "closed_at": "TIMESTAMP",

                "strategy": "TEXT",
                "quality_score": "REAL",
                "atr": "REAL",
                "trading_mode": "TEXT",
                "entry_order_id": "TEXT",
                "stop_order_id": "TEXT",
                "take_profit_order_id": "TEXT",
                "commission": "REAL DEFAULT 0",
                "funding": "REAL DEFAULT 0",
            }

            for column_name, column_type in new_columns.items():
                if column_name not in existing_columns:
                    cursor.execute(
                        f"ALTER TABLE demo_trades "
                        f"ADD COLUMN {column_name} {column_type}"
                    )

            conn.commit()

    def save_open_trade(self, data):
      with self.db.connect() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO demo_trades (
                symbol,
                side,
                entry_price,
                quantity,
                take_profit,
                stop_loss,
                strategy,
                quality_score,
                atr,
                trading_mode,
                entry_order_id,
                stop_order_id,
                take_profit_order_id,
                commission,
                funding,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get("symbol"),
            data.get("side"),
            data.get("entry_price"),
            data.get("quantity"),
            data.get("take_profit"),
            data.get("stop_loss"),
            data.get("strategy", "Unknown"),
            data.get("quality_score", 0),
            data.get("atr", 0),
            data.get("trading_mode", "DEMO"),
            data.get("entry_order_id"),
            data.get("stop_order_id"),
            data.get("take_profit_order_id"),
            data.get("commission", 0),
            data.get("funding", 0),
            data.get("status", "OPEN"),
        ))

        conn.commit()

    def close_last_open_trade(
        self,
        symbol,
        exit_price,
        pnl,
        pnl_percent,
        close_reason,
        status="CLOSED",
        commission=0,
        funding=0,
    ):
        with self.db.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE demo_trades
                SET
                    status = ?,
                    exit_price = ?,
                    pnl = ?,
                    pnl_percent = ?,
                    close_reason = ?,
                    commission = ?,
                    funding = ?,
                    closed_at = CURRENT_TIMESTAMP
                WHERE id = (
                    SELECT id
                    FROM demo_trades
                    WHERE symbol = ?
                    AND status = 'OPEN'
                    ORDER BY id DESC
                    LIMIT 1
                )
            """, (
                status,
                exit_price,
                pnl,
                pnl_percent,
                close_reason,
                commission,
                funding,
                symbol,
            ))

            conn.commit()
            print(f"Rows updated: {cursor.rowcount}")

    def update_protection(
        self,
        symbol,
        stop_loss=None,
        stop_order_id=None,
        take_profit_order_id=None,
    ):
        assignments = []
        values = []
        for column, value in (
            ("stop_loss", stop_loss),
            ("stop_order_id", stop_order_id),
            ("take_profit_order_id", take_profit_order_id),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        if not assignments:
            return
        values.append(symbol)
        with self.db.connect() as conn:
            conn.execute(
                f"""
                UPDATE demo_trades SET {", ".join(assignments)}
                WHERE id = (
                    SELECT id FROM demo_trades
                    WHERE symbol = ? AND status = 'OPEN'
                    ORDER BY id DESC LIMIT 1
                )
                """,
                values,
            )

    def mark_last_open_trade(self, symbol, status, close_reason):
        if status not in {"ORPHANED", "ERROR"}:
            raise ValueError(f"Недопустимый статус: {status}")

        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE demo_trades
                SET status = ?, close_reason = ?, closed_at = CURRENT_TIMESTAMP
                WHERE id = (
                    SELECT id FROM demo_trades
                    WHERE symbol = ? AND status = 'OPEN'
                    ORDER BY id DESC LIMIT 1
                )
                """,
                (status, close_reason, symbol),
            )

    def get_last_open_trade(self, symbol):
        with self.db.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                id,
                    symbol,
                    side,
                    entry_price,
                    quantity,
                    take_profit,
                    stop_loss,
                    status,
                    trading_mode,
                    entry_order_id,
                    stop_order_id,
                    take_profit_order_id,
                    commission,
                    funding
                    ,created_at
                FROM demo_trades
                WHERE symbol = ?
                AND status = 'OPEN'
                ORDER BY id DESC
                LIMIT 1
            """, (symbol,))

            row = cursor.fetchone()

            if row is None:
                return None

            return {
                "id": row[0],
                "symbol": row[1],
                "side": row[2],
                "entry_price": row[3],
                "quantity": row[4],
                "take_profit": row[5],
                "stop_loss": row[6],
                "status": row[7],
                "trading_mode": row[8],
                "entry_order_id": row[9],
                "stop_order_id": row[10],
                "take_profit_order_id": row[11],
                "commission": row[12],
                "funding": row[13],
                "created_at": row[14],
            }

    def delete_all_trades(self):
        with self.db.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM demo_trades")
            conn.commit()

    def get_recent_trades(self, limit=10):
      with self.db.connect() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                id,
                symbol,
                side,
                entry_price,
                exit_price,
                quantity,
                pnl,
                pnl_percent,
                status,
                close_reason,
                strategy,
                quality_score,
                atr,
                created_at,
                closed_at
            FROM demo_trades
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))

        rows = cursor.fetchall()

        trades = []

        for row in rows:
            trades.append({
                "id": row[0],
                "symbol": row[1],
                "side": row[2],
                "entry_price": row[3],
                "exit_price": row[4],
                "quantity": row[5],
                "pnl": row[6],
                "pnl_percent": row[7],
                "status": row[8],
                "close_reason": row[9],
                "strategy": row[10],
                "quality_score": row[11],
                "atr": row[12],
                "created_at": row[13],
                "closed_at": row[14],
            })

        return trades

    def get_open_trades(self):
        with self.db.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    id,
                    symbol,
                    side,
                    entry_price,
                    quantity,
                    take_profit,
                    stop_loss,
                    status,
                    strategy,
                    quality_score,
                    atr,
                    created_at
                    ,trading_mode
                    ,entry_order_id
                    ,stop_order_id
                    ,take_profit_order_id
                    ,commission
                    ,funding
                FROM demo_trades
                WHERE status = 'OPEN'
                ORDER BY id ASC
            """)

            rows = cursor.fetchall()

            trades = []

            for row in rows:
                trades.append({
                    "id": row[0],
                    "symbol": row[1],
                    "side": row[2],
                    "entry_price": row[3],
                    "quantity": row[4],
                    "take_profit": row[5],
                    "stop_loss": row[6],
                    "status": row[7],
                    "strategy": row[8],
                    "quality_score": row[9],
                    "atr": row[10],
                    "created_at": row[11],
                    "trading_mode": row[12],
                    "entry_order_id": row[13],
                    "stop_order_id": row[14],
                    "take_profit_order_id": row[15],
                    "commission": row[16],
                    "funding": row[17],
                })

            return trades
