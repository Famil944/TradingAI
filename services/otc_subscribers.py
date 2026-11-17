"""Private opt-in subscribers; relay only allowlisted forecast fields.

Never opens the owner's wallet and never copies the owner's Telegram message.
Joining a public Telegram channel is handled by Telegram; this store is for
people who send /start to the bot in a private chat.
"""
import asyncio
import hashlib
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from services.otc_public_broadcast import public_text
from services.otc_public_stats import main_signals

LOG = logging.getLogger(__name__)

# The owner gets original private cards through the existing strategy.
# A subscriber is never sent a stale, already-entered signal as a new entry.
MIN_ENTRY_LEAD_SECONDS = 3
MAX_SEND_AGE_SECONDS = 90
POLL_SECONDS = 2
MAX_USERS_PER_TICK = 200


class Subscribers:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS subscribers(
                    chat_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    subscribed_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries(
                    chat_id INTEGER NOT NULL,
                    signal_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    content_sha TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(chat_id,signal_id)
                );
                CREATE INDEX IF NOT EXISTS deliveries_signal_id
                    ON deliveries(signal_id);
            """)
            fields = {row[1] for row in db.execute("PRAGMA table_info(deliveries)")}
            if "content_sha" not in fields:
                db.execute(
                    "ALTER TABLE deliveries ADD COLUMN content_sha TEXT NOT NULL DEFAULT ''"
                )
            db.commit()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def subscribe(self, chat_id, now=None):
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO subscribers(chat_id,enabled,subscribed_at,updated_at)"
                " VALUES(?,1,?,?)"
                " ON CONFLICT(chat_id) DO UPDATE SET "
                " enabled=1,"
                " subscribed_at=CASE WHEN enabled=0 THEN excluded.subscribed_at"
                " ELSE subscribed_at END,"
                " updated_at=excluded.updated_at",
                (chat_id, now, now),
            )
            db.commit()

    def unsubscribe(self, chat_id, now=None):
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            db.execute(
                "UPDATE subscribers SET enabled=0,updated_at=? WHERE chat_id=?",
                (now, chat_id),
            )
            db.commit()

    def active(self, limit=MAX_USERS_PER_TICK, offset=0):
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT chat_id,subscribed_at FROM subscribers "
                "WHERE enabled=1 ORDER BY chat_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()

    def min_active_since(self):
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT MIN(subscribed_at) FROM subscribers WHERE enabled=1"
            ).fetchone()[0]

    def delivered(self, chat_id, signal_id):
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT message_id,outcome,content_sha FROM deliveries "
                "WHERE chat_id=? AND signal_id=?",
                (chat_id, signal_id),
            ).fetchone()

    def saved(self, chat_id, signal_id, message_id, outcome, content_sha):
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO deliveries(chat_id,signal_id,message_id,outcome,content_sha) "
                "VALUES(?,?,?,?,?) ON CONFLICT(chat_id,signal_id) "
                "DO UPDATE SET message_id=excluded.message_id,outcome=excluded.outcome,"
                "content_sha=excluded.content_sha",
                (chat_id, signal_id, message_id, outcome, content_sha),
            )
            db.commit()


class SubscriberRelay:
    def __init__(self, root, store=None, clock=time.time):
        self.root = Path(root)
        self.store = store or Subscribers(self.root / "data" / "subscribers.db")
        self.clock = clock

    def _signals(self):
        since = self.store.min_active_since()
        if since is None:
            return []
        return main_signals(
            self.root / "data" / "bollinger-cycles.db",
            max(0, since - 120, self.clock() - 7200),
        )

    async def update(self, bot):
        rows = await asyncio.to_thread(self._signals)
        if not rows:
            return
        offset = 0
        while True:
            subscribers = await asyncio.to_thread(
                self.store.active, MAX_USERS_PER_TICK, offset
            )
            if not subscribers:
                break
            offset += len(subscribers)
            for chat_id, joined_at in subscribers:
                for row in rows:
                    current_time = self.clock()
                    safe_text = public_text(row)
                    content_sha = hashlib.sha256(safe_text.encode("utf-8")).hexdigest()
                    # Only explicitly delivered main Bollinger forecasts; background
                    # research stays in the read-only stats, not a live signal feed.
                    if row["origin"] != "main":
                        continue
                    already = await asyncio.to_thread(
                        self.store.delivered, chat_id, row["id"]
                    )
                    if already:
                        if already[1] == row["outcome"] and already[2] == content_sha:
                            continue
                        try:
                            await bot.edit_message_text(
                                safe_text, chat_id=chat_id,
                                message_id=already[0], parse_mode=None,
                            )
                            await asyncio.to_thread(
                                self.store.saved, chat_id, row["id"],
                                already[0], row["outcome"], content_sha,
                            )
                        except TelegramForbiddenError:
                            await asyncio.to_thread(self.store.unsubscribe, chat_id)
                            break
                        except TelegramBadRequest as exc:
                            if "message is not modified" in str(exc).lower():
                                await asyncio.to_thread(
                                    self.store.saved, chat_id, row["id"],
                                    already[0], row["outcome"], content_sha,
                                )
                            else:
                                LOG.warning("Subscriber edit rejected for %d", chat_id)
                        except Exception as exc:
                            LOG.warning("Subscriber edit failed: %s", type(exc).__name__)
                        continue
                    if (
                        row["created_at"] < joined_at
                        or row["sent_at"] < joined_at
                        or row["sent_at"] > current_time
                        or current_time - row["sent_at"] > MAX_SEND_AGE_SECONDS
                        or row["entry_at"] - current_time < MIN_ENTRY_LEAD_SECONDS
                    ):
                        continue
                    try:
                        msg = await bot.send_message(
                            chat_id, safe_text, parse_mode=None
                        )
                        await asyncio.to_thread(
                            self.store.saved, chat_id, row["id"],
                            msg.message_id, row["outcome"], content_sha,
                        )
                    except TelegramForbiddenError:
                        await asyncio.to_thread(self.store.unsubscribe, chat_id)
                        break
                    except TelegramBadRequest:
                        LOG.warning("Subscriber message rejected for %d", chat_id)
                    except Exception as exc:
                        LOG.warning("Subscriber delivery failed: %s", type(exc).__name__)

    async def run(self, bot):
        while True:
            try:
                await self.update(bot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Subscriber relay failed: %s", type(exc).__name__)
            await asyncio.sleep(POLL_SECONDS)
