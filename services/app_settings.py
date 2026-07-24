from database.db import Database


DEFAULTS = {
    "risk_percent": "1.0",
    "max_quantity": "0.01",
    "auto_risk": "true",
    "max_balance_percent": "20",
    "quality_score": "70",
    "timeframe": "1h",
    "notifications": "all",
    "auto_enabled": "false",
    "telegram_chat_id": "",
}


class AppSettings:
    """Small persistent runtime settings store shared by Telegram and trading."""

    def __init__(self, database=None):
        self.database = database or Database()
        self._init_table()

    def _init_table(self):
        with self.database.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def get(self, key):
        if key not in DEFAULTS:
            raise KeyError(key)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (key,),
            ).fetchone()
        return row[0] if row else DEFAULTS[key]

    def set(self, key, value):
        value = self._validate(key, value)
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )
        return value

    def snapshot(self):
        return {key: self.get(key) for key in DEFAULTS}

    @staticmethod
    def _validate(key, value):
        if key not in DEFAULTS:
            raise KeyError(key)
        if key == "risk_percent":
            number = float(value)
            if number not in {0.5, 1.0, 2.0}:
                raise ValueError("Допустимый риск: 0.5%, 1% или 2%")
            return str(number)
        if key == "max_quantity":
            number = float(value)
            if number not in {0.001, 0.005, 0.01}:
                raise ValueError("Недопустимый максимальный размер")
            return str(number)
        if key == "auto_risk":
            if str(value).lower() not in {"true", "false"}:
                raise ValueError("auto_risk должен быть true или false")
            return str(value).lower()
        if key == "max_balance_percent":
            number = int(value)
            if not 1 <= number <= 25:
                raise ValueError("Доля баланса должна быть от 1% до 25%")
            return str(number)
        if key == "quality_score":
            number = int(value)
            if number not in {65, 70, 75, 80}:
                raise ValueError("Недопустимый Quality Score")
            return str(number)
        if key == "timeframe":
            if value not in {"15m", "1h", "4h"}:
                raise ValueError("Недопустимый таймфрейм")
            return value
        if key == "notifications":
            if value not in {"all", "trades", "off"}:
                raise ValueError("Недопустимый режим уведомлений")
            return value
        if key == "auto_enabled":
            if str(value).lower() not in {"true", "false"}:
                raise ValueError("auto_enabled должен быть true или false")
            return str(value).lower()
        if key == "telegram_chat_id":
            if value in {"", None}:
                return ""
            return str(int(value))
        raise KeyError(key)
