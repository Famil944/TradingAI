"""Private, owner-approved time-limited subscribers; relay safe forecast fields.

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

from services.otc_public_broadcast import subscriber_card
from services.otc_public_stats import main_signals
from services.otc_strategies_live import load_live_signals

LOG = logging.getLogger(__name__)

# The owner gets original private cards through the existing strategy.
# A subscriber is never sent a stale, already-entered signal as a new entry.
MIN_ENTRY_LEAD_SECONDS = 3
MAX_SEND_AGE_SECONDS = 90
POLL_SECONDS = 2  # fallback/reconciliation only; new signals are pushed immediately
MAX_USERS_PER_TICK = 200
TRIAL_DAYS = 7
SIGNAL_SOURCES = ("main", "bb_reentry", "break_retest", "ema_pullback")
DAY = 86400


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
                CREATE TABLE IF NOT EXISTS source_preferences(
                    chat_id INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(chat_id, source)
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
            # Secure migration: legacy /start opt-ins are NOT paid/approved users.
            # They must explicitly accept the new terms and be approved by owner.
            columns = {row[1] for row in db.execute("PRAGMA table_info(subscribers)")}
            additions = {
                "status": "TEXT NOT NULL DEFAULT 'pending'",
                "accepted_at": "REAL",
                "expires_at": "REAL",
                "trial_used": "INTEGER NOT NULL DEFAULT 0",
                "reminded_at": "REAL",
                "expired_notice_at": "REAL",
            }
            for field, declaration in additions.items():
                if field not in columns:
                    db.execute(f"ALTER TABLE subscribers ADD COLUMN {field} {declaration}")
            # Old enabled flags never bypass the new approval requirement.
            db.execute("UPDATE subscribers SET enabled=0 WHERE status='pending'")
            db.commit()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def request(self, chat_id, now=None):
        """Record explicit consent/request; repeat requests cannot extend a trial."""
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,expires_at FROM subscribers WHERE chat_id=?", (chat_id,)
            ).fetchone()
            if row and row[0] == "blocked":
                return "blocked"
            if row and row[0] == "approved" and row[1] and row[1] > now:
                db.execute(
                    "UPDATE subscribers SET enabled=1,"
                    "subscribed_at=CASE WHEN enabled=0 THEN ? ELSE subscribed_at END,"
                    "updated_at=? WHERE chat_id=?", (now, now, chat_id),
                )
                db.commit()
                return "approved"
            db.execute(
                "INSERT INTO subscribers(chat_id,enabled,subscribed_at,updated_at,"
                "status,accepted_at) VALUES(?,0,?,?,'pending',?)"
                " ON CONFLICT(chat_id) DO UPDATE SET "
                " enabled=0,status='pending',accepted_at=excluded.accepted_at,"
                " updated_at=excluded.updated_at",
                (chat_id, now, now, now),
            )
            db.commit()
            return "pending"

    def subscribe(self, chat_id, now=None):
        """Legacy entry point: requesting is not permission to receive signals."""
        return self.request(chat_id, now)

    def status(self, chat_id, now=None):
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT status,enabled,accepted_at,expires_at,trial_used "
                "FROM subscribers WHERE chat_id=?", (chat_id,)
            ).fetchone()
        if not row:
            return dict(state="new", expires_at=None, trial_used=False)
        state, enabled, accepted_at, expires_at, trial_used = row
        if state == "approved" and (expires_at is None or expires_at <= now):
            state = "expired"
        elif state == "approved" and not enabled:
            state = "paused"
        elif state == "pending" and accepted_at is None:
            state = "new"
        return dict(state=state, expires_at=expires_at, trial_used=bool(trial_used))

    def grant(self, chat_id, days=TRIAL_DAYS, now=None):
        """Owner-only caller. Reapproval/renewal never grants a second free trial."""
        if not isinstance(days, int) or not 1 <= days <= 365:
            raise ValueError("Срок доступа: от 1 до 365 дней")
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,accepted_at,expires_at FROM subscribers WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
            if not row or row[1] is None or row[0] == "blocked":
                db.rollback()
                raise ValueError("Нет принятой заявки или пользователь заблокирован")
            expiry = max(now, row[2] or 0) + days * DAY
            db.execute(
                "UPDATE subscribers SET status='approved',enabled=1,"
                "trial_used=1,expires_at=?,subscribed_at=?,updated_at=?,"
                "reminded_at=NULL,expired_notice_at=NULL WHERE chat_id=?",
                (expiry, now, now, chat_id),
            )
            db.commit()
            return expiry

    def block(self, chat_id, now=None):
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            changed = db.execute(
                "UPDATE subscribers SET status='blocked',enabled=0,updated_at=? "
                "WHERE chat_id=?", (now, chat_id),
            ).rowcount
            db.commit()
            return bool(changed)

    def unblock(self, chat_id, now=None):
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            changed = db.execute(
                "UPDATE subscribers SET status='pending',enabled=0,updated_at=? "
                "WHERE chat_id=? AND status='blocked'", (now, chat_id),
            ).rowcount
            db.commit()
            return bool(changed)

    def members(self, limit=30):
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT chat_id,status,expires_at,trial_used,accepted_at "
                "FROM subscribers ORDER BY "
                "CASE WHEN status='pending' AND accepted_at IS NOT NULL "
                "THEN 0 ELSE 1 END, updated_at DESC LIMIT ?", (limit,)
            ).fetchall()

    def notices_due(self, now=None):
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT chat_id,CASE WHEN expires_at <= ? THEN 'expired' "
                "ELSE 'reminder' END FROM subscribers WHERE status='approved' "
                "AND enabled=1 "
                "AND expires_at IS NOT NULL AND ("
                "(expires_at <= ? AND expired_notice_at IS NULL) OR "
                "(expires_at > ? AND expires_at <= ? AND reminded_at IS NULL)) "
                "ORDER BY expires_at LIMIT 200",
                (now, now, now, now + DAY),
            ).fetchall()

    def notice_sent(self, chat_id, kind, now=None):
        if kind not in ("reminder", "expired"):
            raise ValueError("Invalid notification")
        field = "reminded_at" if kind == "reminder" else "expired_notice_at"
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            db.execute(f"UPDATE subscribers SET {field}=? WHERE chat_id=?", (now, chat_id))
            db.commit()

    def unsubscribe(self, chat_id, now=None):
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            db.execute(
                "UPDATE subscribers SET enabled=0,updated_at=? WHERE chat_id=?",
                (now, chat_id),
            )
            db.commit()

    def active(self, limit=MAX_USERS_PER_TICK, offset=0, now=None):
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT chat_id,subscribed_at FROM subscribers "
                "WHERE enabled=1 AND status='approved' AND accepted_at IS NOT NULL "
                "AND expires_at>? ORDER BY chat_id LIMIT ? OFFSET ?",
                (now, limit, offset),
            ).fetchall()

    def min_active_since(self, now=None):
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT MIN(subscribed_at) FROM subscribers "
                "WHERE enabled=1 AND status='approved' AND accepted_at IS NOT NULL "
                "AND expires_at>?", (now,),
            ).fetchone()[0]

    def source_preferences(self, chat_id):
        """Default: all four published sources enabled; preferences persist per user."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT source,enabled FROM source_preferences WHERE chat_id=?",
                (chat_id,),
            ).fetchall()
        saved = dict(rows)
        return {source: bool(saved.get(source, 1)) for source in SIGNAL_SOURCES}

    def set_source(self, chat_id, source, enabled):
        if source not in SIGNAL_SOURCES:
            raise ValueError("Unknown source")
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO source_preferences(chat_id,source,enabled) VALUES(?,?,?) "
                "ON CONFLICT(chat_id,source) DO UPDATE SET enabled=excluded.enabled",
                (chat_id, source, int(bool(enabled))),
            )
            db.commit()

    def allows_source(self, chat_id, source):
        if source == "bollinger_live":
            source = "main"
        if source not in SIGNAL_SOURCES:
            return False
        return self.source_preferences(chat_id)[source]

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
    def __init__(self, root, store=None, clock=time.time, live_cycle=None):
        self.root = Path(root)
        self.store = store or Subscribers(self.root / "data" / "subscribers.db")
        self.clock = clock
        self.live_cycle = live_cycle
        self._last_notice_check = 0.0

    def _signals(self):
        since = self.store.min_active_since(now=self.clock())
        if since is None:
            return []
        cutoff = max(0, since - 120, self.clock() - 7200)
        return sorted(
            main_signals(self.root / "data" / "bollinger-cycles.db", cutoff)
            + load_live_signals(self.root / "data" / "community-live.db", cutoff),
            key=lambda row: (row["created_at"], row["id"]),
        )

    async def publish_now(self, bot, row):
        """Push one newly owner-delivered signal to all current subscribers now.

        This is the primary delivery path. The polling loop remains a fallback
        and later mirrors WIN/LOSS edits. Subscription age is compared with
        sent_at, not candidate created_at: a candidate may have been calculated
        before /start but only becomes an actionable signal when the owner card
        is actually delivered.
        """
        if row.get("origin") not in ("main", "live"):
            return
        current_time = self.clock()
        sent_at = row.get("sent_at")
        if (
            sent_at is None
            or sent_at > current_time
            or current_time - sent_at > MAX_SEND_AGE_SECONDS
            or row["entry_at"] - current_time < MIN_ENTRY_LEAD_SECONDS
        ):
            return

        safe_text = subscriber_card(row, self.live_cycle)
        content_sha = hashlib.sha256(safe_text.encode("utf-8")).hexdigest()
        offset = 0
        while True:
            subscribers = await asyncio.to_thread(
                self.store.active, MAX_USERS_PER_TICK, offset, current_time
            )
            if not subscribers:
                break
            offset += len(subscribers)

            async def deliver(chat_id, joined_at):
                if (sent_at < joined_at or self.store.status(chat_id, self.clock())["state"] != "approved"
                        or not self.store.allows_source(chat_id, row["strategy"])):
                    return
                already = await asyncio.to_thread(
                    self.store.delivered, chat_id, row["id"]
                )
                if already:
                    return
                try:
                    msg = await bot.send_message(chat_id, safe_text, parse_mode=None)
                    await asyncio.to_thread(
                        self.store.saved, chat_id, row["id"],
                        msg.message_id, row["outcome"], content_sha,
                    )
                except TelegramForbiddenError:
                    await asyncio.to_thread(self.store.unsubscribe, chat_id)
                except TelegramBadRequest:
                    LOG.warning("Subscriber message rejected for %d", chat_id)
                except Exception as exc:
                    LOG.warning(
                        "Subscriber immediate delivery failed: %s",
                        type(exc).__name__,
                    )

            await asyncio.gather(
                *(deliver(chat_id, joined_at) for chat_id, joined_at in subscribers)
            )

    async def update(self, bot):
        rows = await asyncio.to_thread(self._signals)
        if not rows:
            return
        offset = 0
        while True:
            subscribers = await asyncio.to_thread(
                self.store.active, MAX_USERS_PER_TICK, offset, self.clock()
            )
            if not subscribers:
                break
            offset += len(subscribers)
            for chat_id, joined_at in subscribers:
                if self.store.status(chat_id, self.clock())["state"] != "approved":
                    continue
                for row in rows:
                    if not self.store.allows_source(chat_id, row["strategy"]):
                        continue
                    current_time = self.clock()
                    safe_text = subscriber_card(row, self.live_cycle)
                    content_sha = hashlib.sha256(safe_text.encode("utf-8")).hexdigest()
                    # All four published strategies, never legacy research rows.
                    if row["origin"] not in ("main", "live"):
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
                        row["sent_at"] < joined_at
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

    async def notify_expiry(self, bot):
        """Send a 24-hour reminder and a one-off expiration notice."""
        due = await asyncio.to_thread(self.store.notices_due, self.clock())
        for chat_id, kind in due:
            content = (
                "⏳ До конца доступа меньше 24 часов. "
                "Для продления обратитесь к владельцу бота."
                if kind == "reminder" else
                "⌛ Доступ закончился. Новые сигналы больше не отправляются. "
                "Продление — по решению владельца. /start"
            )
            try:
                await bot.send_message(chat_id, content, parse_mode=None)
                await asyncio.to_thread(self.store.notice_sent, chat_id, kind, self.clock())
            except TelegramForbiddenError:
                await asyncio.to_thread(self.store.unsubscribe, chat_id)
                await asyncio.to_thread(self.store.notice_sent, chat_id, kind, self.clock())
            except Exception as exc:
                LOG.warning("Subscriber expiry notice failed: %s", type(exc).__name__)

    async def run(self, bot):
        while True:
            try:
                await self.update(bot)
                current = self.clock()
                if current - self._last_notice_check >= 60:
                    self._last_notice_check = current
                    await self.notify_expiry(bot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Subscriber relay failed: %s", type(exc).__name__)
            await asyncio.sleep(POLL_SECONDS)
