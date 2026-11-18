"""Private owner announcements and configurable inline polls, isolated from OTC logic."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class AnnouncementStore:
    """Persistent send queue and single-choice votes (not trading signals)."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS announcements(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK(kind IN ('notice','poll')),
                    body TEXT NOT NULL,
                    options_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS announcement_deliveries(
                    announcement_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(announcement_id,chat_id)
                );
                CREATE INDEX IF NOT EXISTS announcement_delivery_state
                    ON announcement_deliveries(announcement_id,state);
                CREATE TABLE IF NOT EXISTS announcement_votes(
                    announcement_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    option_index INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(announcement_id,chat_id)
                );
            """)
            db.commit()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def validate(kind, body, options):
        body = str(body).strip()
        if kind not in ("notice", "poll") or not 1 <= len(body) <= 1000:
            raise ValueError("Текст должен содержать от 1 до 1000 символов.")
        opts = [str(o).strip() for o in options]
        if kind == "notice":
            if opts:
                raise ValueError("У объявления нет вариантов ответа.")
        elif not (2 <= len(opts) <= 8 and all(1 <= len(o) <= 75 for o in opts)
                  and len({o.casefold() for o in opts}) == len(opts)):
            raise ValueError("Нужно 2–8 разных вариантов, каждый до 75 символов.")
        return body, opts

    def create(self, kind, body, options, recipients, now=None):
        body, options = self.validate(kind, body, options)
        when = time.time() if now is None else float(now)
        targets = sorted({int(x) for x in recipients if int(x) > 0})
        if not targets:
            raise ValueError("Нет подписчиков с активным доступом.")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute(
                "INSERT INTO announcements(kind,body,options_json,created_at)"
                " VALUES(?,?,?,?)", (kind, body, json.dumps(options, ensure_ascii=False), when),
            )
            aid = int(cur.lastrowid)
            db.executemany(
                "INSERT INTO announcement_deliveries(announcement_id,chat_id,state,updated_at)"
                " VALUES(?,?,'pending',?)",
                [(aid, chat_id, when) for chat_id in targets],
            )
            db.commit()
        return aid

    def get(self, aid):
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM announcements WHERE id=?", (int(aid),)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["options"] = json.loads(result.pop("options_json"))
        return result

    def recent(self, limit=10):
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT id,kind,body,created_at FROM announcements ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 20)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_ids(self):
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT DISTINCT announcement_id FROM announcement_deliveries"
                " WHERE state='pending' ORDER BY announcement_id"
            ).fetchall()
        return [int(row[0]) for row in rows]

    def pending(self, aid, limit=100):
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT chat_id FROM announcement_deliveries"
                " WHERE announcement_id=? AND state='pending' ORDER BY chat_id LIMIT ?",
                (int(aid), max(1, int(limit))),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def delivery_state(self, aid, chat_id, state, now=None):
        if state not in ("sent", "failed", "skipped"):
            raise ValueError("Invalid delivery state")
        when = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            changed = db.execute(
                "UPDATE announcement_deliveries SET state=?,updated_at=?"
                " WHERE announcement_id=? AND chat_id=? AND state='pending'",
                (state, when, int(aid), int(chat_id)),
            ).rowcount
            db.commit()
        return changed == 1

    def retry_failed(self, aid):
        when = time.time()
        with closing(self._connect()) as db:
            changed = db.execute(
                "UPDATE announcement_deliveries SET state='pending',updated_at=?"
                " WHERE announcement_id=? AND state='failed'", (when, int(aid)),
            ).rowcount
            db.commit()
        return changed

    def vote(self, aid, chat_id, index, now=None):
        when = time.time() if now is None else float(now)
        aid, chat_id, index = int(aid), int(chat_id), int(index)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            poll = db.execute(
                "SELECT kind,options_json FROM announcements WHERE id=?", (aid,),
            ).fetchone()
            target = db.execute(
                "SELECT 1 FROM announcement_deliveries"
                " WHERE announcement_id=? AND chat_id=?",
                (aid, chat_id),
            ).fetchone()
            if (poll is None or poll["kind"] != "poll" or not target
                    or index < 0 or index >= len(json.loads(poll["options_json"]))):
                db.rollback()
                return False
            db.execute(
                "INSERT INTO announcement_votes(announcement_id,chat_id,option_index,updated_at)"
                " VALUES(?,?,?,?) ON CONFLICT(announcement_id,chat_id)"
                " DO UPDATE SET option_index=excluded.option_index,updated_at=excluded.updated_at",
                (aid, chat_id, index, when),
            )
            db.commit()
        return True

    def selected(self, aid, chat_id):
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT option_index FROM announcement_votes"
                " WHERE announcement_id=? AND chat_id=?", (int(aid), int(chat_id)),
            ).fetchone()
        return int(row[0]) if row else None

    def summary(self, aid):
        entry = self.get(aid)
        if entry is None:
            return None
        with closing(self._connect()) as db:
            states = db.execute(
                "SELECT state,COUNT(*) AS n FROM announcement_deliveries"
                " WHERE announcement_id=? GROUP BY state", (int(aid),),
            ).fetchall()
            votes = db.execute(
                "SELECT option_index,COUNT(*) AS n FROM announcement_votes"
                " WHERE announcement_id=? GROUP BY option_index", (int(aid),),
            ).fetchall()
        entry["delivery"] = {s["state"]: int(s["n"]) for s in states}
        entry["votes"] = [0] * len(entry["options"])
        for v in votes:
            if 0 <= v["option_index"] < len(entry["votes"]):
                entry["votes"][v["option_index"]] = int(v["n"])
        return entry
