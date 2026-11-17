"""Private, persistent suggestion inbox, independent of any trading account."""
import sqlite3
import time
from contextlib import closing
from pathlib import Path

STATUSES = ("NEW", "REVIEW", "ACCEPTED", "REJECTED", "DONE")
LABELS = {
    "NEW": "Новое", "REVIEW": "На рассмотрении",
    "ACCEPTED": "Принято", "REJECTED": "Отклонено", "DONE": "Реализовано",
}


class FeedbackStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS suggestions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL, created_at REAL NOT NULL,
                    text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'NEW',
                    updated_at REAL NOT NULL,
                    UNIQUE(chat_id,message_id)
                );
                CREATE INDEX IF NOT EXISTS suggestion_sender_created
                    ON suggestions(sender_id,created_at);
                CREATE TABLE IF NOT EXISTS feedback_state(
                    user_id INTEGER PRIMARY KEY, mode TEXT NOT NULL,
                    suggestion_id INTEGER, updated_at REAL NOT NULL
                );
            """)
            db.commit()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def begin(self, user_id, mode="SUGGEST", suggestion_id=None, now=None):
        if mode not in ("SUGGEST", "REPLY"):
            raise ValueError("Unsupported mode")
        now = time.time() if now is None else now
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO feedback_state(user_id,mode,suggestion_id,updated_at)"
                " VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET"
                " mode=excluded.mode,suggestion_id=excluded.suggestion_id,"
                " updated_at=excluded.updated_at",
                (user_id, mode, suggestion_id, now),
            )
            db.commit()

    def state(self, user_id):
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT mode,suggestion_id,updated_at FROM feedback_state WHERE user_id=?",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def cancel(self, user_id):
        with closing(self._connect()) as db:
            db.execute("DELETE FROM feedback_state WHERE user_id=?", (user_id,))
            db.commit()

    def submit(self, user_id, chat_id, message_id, body, now=None):
        now = time.time() if now is None else now
        body = body.strip()
        if not 5 <= len(body) <= 1000:
            raise ValueError("Отправь предложение длиной от 5 до 1000 символов.")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT id FROM suggestions WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            ).fetchone()
            if previous:
                return int(previous["id"])
            last = db.execute(
                "SELECT MAX(created_at) FROM suggestions WHERE sender_id=?",
                (user_id,),
            ).fetchone()[0]
            if last is not None and now - last < 600:
                raise ValueError("Следующее предложение можно отправить через 10 минут.")
            cursor = db.execute(
                "INSERT INTO suggestions(sender_id,chat_id,message_id,created_at,text,status,updated_at)"
                " VALUES(?,?,?,?,?,'NEW',?)",
                (user_id, chat_id, message_id, now, body, now),
            )
            db.execute("DELETE FROM feedback_state WHERE user_id=?", (user_id,))
            db.commit()
            return int(cursor.lastrowid)

    def list(self, limit=15, status=None):
        if status is not None and status not in STATUSES:
            raise ValueError("Unsupported status")
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT id,created_at,text,status FROM suggestions"
                " WHERE (? IS NULL OR status=?) ORDER BY id DESC LIMIT ?",
                (status, status, max(1, min(limit, 50))),
            ).fetchall()
            return [dict(row) for row in rows]

    def get(self, identifier):
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM suggestions WHERE id=?", (identifier,)
            ).fetchone()
            return dict(row) if row else None

    def set_status(self, identifier, status, now=None):
        if status not in STATUSES:
            raise ValueError("Unsupported status")
        now = time.time() if now is None else now
        with closing(self._connect()) as db:
            cursor = db.execute(
                "UPDATE suggestions SET status=?,updated_at=? WHERE id=?",
                (status, now, identifier),
            )
            db.commit()
            return cursor.rowcount == 1
