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
from datetime import datetime, timedelta, time as dt_time
from contextlib import closing
from zoneinfo import ZoneInfo
from pathlib import Path

from aiogram import BaseMiddleware, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from services.otc_public_broadcast import subscriber_card
from services.otc_public_stats import main_signals
from services.otc_strategies_live import load_live_signals
from services.otc_delivery_timing import MIN_LEAD, in_window, send_timeout

LOG = logging.getLogger(__name__)

# The owner gets original private cards through the existing strategy.
# A subscriber is never sent a stale, already-entered signal as a new entry.
MIN_ENTRY_LEAD_SECONDS = MIN_LEAD
MAX_SEND_AGE_SECONDS = 90
POLL_SECONDS = 2  # fallback/reconciliation only; new signals are pushed immediately
MAX_USERS_PER_TICK = 200
TRIAL_DAYS = 7
SIGNAL_SOURCES = ("main", "bb_reentry", "break_retest", "ema_pullback")
SIGNAL_TIMEFRAMES = (120, 300, 900)  # M2, M5, M15; subscriber delivery only.
DAY = 86400
SESSION_SECONDS = 3600
SESSION_REMINDER_SECONDS = 600
MSK = ZoneInfo("Europe/Moscow")


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
                CREATE TABLE IF NOT EXISTS subscriber_sessions(
                    chat_id INTEGER PRIMARY KEY,
                    started_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    reminded_at REAL
                );
                CREATE TABLE IF NOT EXISTS subscriber_profiles(
                    chat_id INTEGER PRIMARY KEY,
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    username TEXT,
                    last_interaction REAL
                );
                CREATE TABLE IF NOT EXISTS subscriber_activity_daily(
                    chat_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    actions INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(chat_id, day)
                );
                CREATE TABLE IF NOT EXISTS source_preferences(
                    chat_id INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(chat_id, source)
                );
                CREATE TABLE IF NOT EXISTS timeframe_preferences(
                    chat_id INTEGER NOT NULL,
                    seconds INTEGER NOT NULL CHECK(seconds IN (120,300,900)),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(chat_id,seconds)
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
                CREATE TABLE IF NOT EXISTS delivery_attempts(
                    chat_id INTEGER NOT NULL,
                    signal_id TEXT NOT NULL,
                    attempted_at REAL NOT NULL,
                    PRIMARY KEY(chat_id,signal_id)
                );
                CREATE TABLE IF NOT EXISTS access_grants(
                    request_key TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    days INTEGER NOT NULL,
                    granted_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
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

    def grant(self, chat_id, days=TRIAL_DAYS, now=None, *, request_key=None, approval_only=False):
        """Owner-only caller. Reapproval/renewal never grants a second free trial."""
        if not isinstance(days, int) or not 1 <= days <= 365:
            raise ValueError("Срок доступа: от 1 до 365 дней")
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if request_key is not None and db.execute(
                "SELECT 1 FROM access_grants WHERE request_key=?", (request_key,)
            ).fetchone():
                raise ValueError("Эта выдача доступа уже выполнена. Для нового продления откройте карточку пользователя заново.")
            row = db.execute(
                "SELECT status,accepted_at,expires_at FROM subscribers WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
            if not row or row[1] is None or row[0] == "blocked":
                db.rollback()
                raise ValueError("Нет принятой заявки или пользователь заблокирован")
            if approval_only and row[0] == "approved" and row[2] and row[2] > now:
                raise ValueError("Доступ уже одобрен. Продление доступно отдельно в карточке пользователя.")
            expiry = max(now, row[2] or 0) + days * DAY
            db.execute(
                "UPDATE subscribers SET status='approved',enabled=1,"
                "trial_used=1,expires_at=?,subscribed_at=?,updated_at=?,"
                "reminded_at=NULL,expired_notice_at=NULL WHERE chat_id=?",
                (expiry, now, now, chat_id),
            )
            if request_key is not None:
                db.execute(
                    "INSERT INTO access_grants VALUES(?,?,?,?,?)",
                    (request_key, chat_id, days, now, expiry),
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

    def record_interaction(self, chat_id, first_name="", last_name="", username=None, now=None):
        """Only consented users; record actions, not message text or reading time."""
        now = time.time() if now is None else float(now)
        day = datetime.fromtimestamp(now, MSK).date().isoformat()
        with closing(self._connect()) as db:
            if not db.execute(
                "SELECT 1 FROM subscribers WHERE chat_id=? AND accepted_at IS NOT NULL",
                (chat_id,),
            ).fetchone():
                return False
            db.execute(
                "INSERT INTO subscriber_profiles(chat_id,first_name,last_name,username,last_interaction) "
                "VALUES(?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET "
                "first_name=excluded.first_name,last_name=excluded.last_name,"
                "username=excluded.username,last_interaction=excluded.last_interaction",
                (chat_id, str(first_name or "")[:100], str(last_name or "")[:100],
                 str(username)[:32] if username else None, now),
            )
            db.execute(
                "INSERT INTO subscriber_activity_daily(chat_id,day,actions) VALUES(?,?,1) "
                "ON CONFLICT(chat_id,day) DO UPDATE SET actions=actions+1",
                (chat_id, day),
            )
            db.commit()
        return True

    def start_session(self, chat_id, now=None):
        """Explicitly begin/renew personal notifications, never the OTC engine."""
        now = time.time() if now is None else float(now)
        local_date = datetime.fromtimestamp(now, MSK).date()
        next_midnight = datetime.combine(
            local_date + timedelta(days=1), dt_time.min, MSK
        ).timestamp()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            subscriber = db.execute(
                "SELECT status,enabled,accepted_at,expires_at FROM subscribers WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
            if (
                subscriber is None or subscriber[0] != "approved"
                or not subscriber[1] or subscriber[2] is None
                or subscriber[3] is None or subscriber[3] <= now
            ):
                db.rollback()
                raise ValueError("Нет активного доступа. Проверь подписку через /start.")
            expires = min(now + SESSION_SECONDS, next_midnight, subscriber[3])
            old = db.execute(
                "SELECT started_at,expires_at FROM subscriber_sessions WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
            started = old[0] if old and old[1] > now else now
            db.execute(
                "INSERT INTO subscriber_sessions(chat_id,started_at,expires_at,reminded_at) "
                "VALUES(?,?,?,NULL) ON CONFLICT(chat_id) DO UPDATE SET "
                "started_at=excluded.started_at,expires_at=excluded.expires_at,"
                "reminded_at=NULL",
                (chat_id, started, expires),
            )
            db.commit()
        return expires

    def stop_session(self, chat_id, now=None):
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            changed = db.execute(
                "UPDATE subscriber_sessions SET expires_at=?,reminded_at=NULL "
                "WHERE chat_id=?", (now, chat_id),
            ).rowcount
            db.commit()
        return bool(changed)

    def session_status(self, chat_id, now=None):
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT started_at,expires_at FROM subscriber_sessions WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
        if row is None or row[0] > now or row[1] <= now:
            return dict(active=False, expires_at=None)
        return dict(active=True, expires_at=row[1])

    def session_allows_signal(self, chat_id, sent_at, now=None):
        """No backfilling signals emitted before a user's current active session."""
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT started_at,expires_at FROM subscriber_sessions WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
        return bool(row and row[0] <= sent_at <= now < row[1])

    def session_reminders_due(self, now=None):
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT s.chat_id,t.expires_at FROM subscriber_sessions t "
                "JOIN subscribers s ON s.chat_id=t.chat_id "
                "WHERE s.status='approved' AND s.enabled=1 AND s.accepted_at IS NOT NULL "
                "AND s.expires_at>? AND t.expires_at>? AND t.expires_at<=? "
                "AND t.reminded_at IS NULL ORDER BY t.expires_at LIMIT 100",
                (now, now, now + SESSION_REMINDER_SECONDS),
            ).fetchall()

    def mark_session_reminded(self, chat_id, expires_at, now=None):
        now = time.time() if now is None else float(now)
        with closing(self._connect()) as db:
            db.execute(
                "UPDATE subscriber_sessions SET reminded_at=? "
                "WHERE chat_id=? AND expires_at=? AND reminded_at IS NULL",
                (now, chat_id, expires_at),
            )
            db.commit()

    def member_profiles(self, limit=30, offset=0):
        """Existing members, with last known public Telegram profile and delivery count."""
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT s.chat_id,s.status,s.expires_at,s.trial_used,s.accepted_at,"
                "p.first_name,p.last_name,p.username,p.last_interaction,"
                "t.expires_at AS session_expires_at,"
                "(SELECT COUNT(*) FROM deliveries d WHERE d.chat_id=s.chat_id) AS delivered "
                "FROM subscribers s LEFT JOIN subscriber_profiles p ON p.chat_id=s.chat_id "
                "LEFT JOIN subscriber_sessions t ON t.chat_id=s.chat_id "
                "ORDER BY CASE WHEN s.status='pending' AND s.accepted_at IS NOT NULL "
                "THEN 0 ELSE 1 END, s.updated_at DESC,s.chat_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def dashboard_counts(self, now=None):
        now = time.time() if now is None else now
        with closing(self._connect()) as db:
            pending, active, sessions = db.execute(
                "SELECT COALESCE(SUM(s.status='pending' AND s.accepted_at IS NOT NULL),0),"
                "COALESCE(SUM(s.status='approved' AND s.expires_at>?),0),"
                "COALESCE(SUM(s.status='approved' AND s.enabled=1 AND s.expires_at>? "
                "AND t.expires_at>?),0) FROM subscribers s "
                "LEFT JOIN subscriber_sessions t ON t.chat_id=s.chat_id", (now, now, now)
            ).fetchone()
            attempted, confirmed = db.execute(
                "SELECT COUNT(*),COUNT(d.signal_id) FROM delivery_attempts a "
                "LEFT JOIN deliveries d ON d.chat_id=a.chat_id AND d.signal_id=a.signal_id "
                "WHERE a.attempted_at>=?", (now - DAY,)
            ).fetchone()
        return dict(pending=pending, active=active, sessions=sessions,
                    attempted=attempted, confirmed=confirmed)

    def profiles(self, chat_ids):
        """Fetch existing opt-in Telegram profile labels for an owner-only view.

        Does not store extra personal data or call the Telegram API.
        """
        ids = sorted({int(value) for value in chat_ids})
        if not ids:
            return {}
        if len(ids) > 100:
            raise ValueError("Too many profiles in one request")
        marks = ",".join("?" for _ in ids)
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                f"SELECT chat_id,first_name,last_name,username "
                f"FROM subscriber_profiles WHERE chat_id IN ({marks})",
                ids,
            ).fetchall()
        return {int(row["chat_id"]): dict(row) for row in rows}

    def member_activity(self, chat_id, now=None):
        """Activity means button/command usage, not minutes viewing forecasts."""
        now = time.time() if now is None else float(now)
        today = datetime.fromtimestamp(now, MSK).date()
        day7 = (today - timedelta(days=6)).isoformat()
        day30 = (today - timedelta(days=29)).isoformat()
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            profile = db.execute(
                "SELECT s.chat_id,s.status,s.expires_at,p.first_name,p.last_name,"
                "p.username,p.last_interaction,t.expires_at AS session_expires_at "
                "FROM subscribers s LEFT JOIN subscriber_profiles p ON p.chat_id=s.chat_id "
                "LEFT JOIN subscriber_sessions t ON t.chat_id=s.chat_id "
                "WHERE s.chat_id=?", (chat_id,),
            ).fetchone()
            if profile is None:
                return None
            usage = db.execute(
                "SELECT COALESCE(SUM(CASE WHEN day>=? THEN actions ELSE 0 END),0),"
                "COUNT(DISTINCT CASE WHEN day>=? THEN day END),"
                "COUNT(DISTINCT CASE WHEN day>=? THEN day END) "
                "FROM subscriber_activity_daily WHERE chat_id=?",
                (day7, day7, day30, chat_id),
            ).fetchone()
            delivered = db.execute(
                "SELECT COUNT(*) FROM deliveries WHERE chat_id=?", (chat_id,),
            ).fetchone()[0]
        return dict(profile) | {
            "actions_7d": int(usage[0]), "days_7d": int(usage[1]),
            "days_30d": int(usage[2]), "delivered": int(delivered),
        }

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

    def timeframe_preferences(self, chat_id):
        """Personal M2/M5/M15 delivery choices; all enabled by default.

        This does not control strategy generation, evaluation or public stats.
        """
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT seconds,enabled FROM timeframe_preferences WHERE chat_id=?",
                (int(chat_id),),
            ).fetchall()
        saved = dict(rows)
        return {seconds: bool(saved.get(seconds, 1))
                for seconds in SIGNAL_TIMEFRAMES}

    def set_timeframe(self, chat_id, seconds, enabled):
        if isinstance(seconds, bool) or seconds not in SIGNAL_TIMEFRAMES:
            raise ValueError("Unknown subscriber timeframe")
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO timeframe_preferences(chat_id,seconds,enabled) "
                "VALUES(?,?,?) ON CONFLICT(chat_id,seconds) "
                "DO UPDATE SET enabled=excluded.enabled",
                (int(chat_id), int(seconds), int(bool(enabled))),
            )
            db.commit()

    def allows_timeframe(self, chat_id, seconds):
        return self.timeframe_preferences(chat_id).get(seconds, False)

    def delivered(self, chat_id, signal_id):
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT message_id,outcome,content_sha FROM deliveries "
                "WHERE chat_id=? AND signal_id=?",
                (chat_id, signal_id),
            ).fetchone()

    def delivered_ids(self, chat_id):
        with closing(self._connect()) as db:
            return {r[0] for r in db.execute(
                "SELECT signal_id FROM deliveries WHERE chat_id=?", (chat_id,)
            )}

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

    def claim_delivery(self, chat_id, signal_id, now):
        """Reserve before network I/O; ambiguous failures must not be retried."""
        with closing(self._connect()) as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO delivery_attempts(chat_id,signal_id,attempted_at) "
                "SELECT ?,?,? WHERE NOT EXISTS (SELECT 1 FROM deliveries "
                "WHERE chat_id=? AND signal_id=?)",
                (chat_id, signal_id, now, chat_id, signal_id),
            )
            db.commit()
            return cursor.rowcount == 1

class SubscriberActivityMiddleware(BaseMiddleware):
    """Observe only accepted users' private interactions, never their reading time."""

    def __init__(self, store):
        self.store = store

    async def __call__(self, handler, event: types.Update, data):
        result = await handler(event, data)
        message = getattr(event, "message", None)
        callback = getattr(event, "callback_query", None)
        if callback is not None:
            message = callback.message
        user = (callback.from_user if callback else getattr(message, "from_user", None))
        chat = getattr(message, "chat", None)
        if not user or not chat or chat.type != "private" or user.id != chat.id:
            return result
        try:
            await asyncio.to_thread(
                self.store.record_interaction, chat.id,
                user.first_name, user.last_name, user.username,
            )
        except Exception:
            LOG.exception("Subscriber activity counter failed")
        return result


class SubscriberRelay:
    def __init__(self, root, store=None, clock=time.time, live_cycle=None,
                 require_sessions=False):
        self.root = Path(root)
        self.store = store or Subscribers(self.root / "data" / "subscribers.db")
        self.clock = clock
        self.live_cycle = live_cycle
        self.require_sessions = require_sessions
        self._last_notice_check = 0.0
        self._send_slots = asyncio.Semaphore(20)

    def _eligible(self, chat_id, joined_at, row):
        now = self.clock()
        sent_at = row.get("sent_at")
        return (
            row.get("origin") in ("main", "live")
            and row.get("outcome") == "PENDING"
            and row.get("seconds") in SIGNAL_TIMEFRAMES
            and sent_at is not None and joined_at <= sent_at <= now
            and now - sent_at <= MAX_SEND_AGE_SECONDS
            and in_window(row["entry_at"], now)
            and self.store.status(chat_id, now)["state"] == "approved"
            and self.store.allows_source(chat_id, row["strategy"])
            and self.store.allows_timeframe(chat_id, row["seconds"])
            and (not self.require_sessions or self.store.session_allows_signal(
                chat_id, sent_at, now
            ))
        )

    async def _deliver(self, bot, chat_id, joined_at, row, content, content_sha):
        async with self._send_slots:
            if not await asyncio.to_thread(self._eligible, chat_id, joined_at, row):
                return
            if not await asyncio.to_thread(
                self.store.claim_delivery, chat_id, row["id"], self.clock()
            ):
                return
            timeout = send_timeout(row["entry_at"], self.clock())
            if timeout <= 0:
                return
            try:
                msg = await asyncio.wait_for(
                    bot.send_message(chat_id, content, parse_mode=None), timeout
                )
                if not in_window(row["entry_at"], self.clock()):
                    await asyncio.wait_for(bot.edit_message_text(
                        "⚠️ Сигнал опоздал — НЕ ВХОДИТЬ.",
                        chat_id=chat_id, message_id=msg.message_id, parse_mode=None,
                    ), 3)
                    return
                await asyncio.to_thread(
                    self.store.saved, chat_id, row["id"],
                    msg.message_id, row["outcome"], content_sha,
                )
            except TelegramForbiddenError:
                await asyncio.to_thread(self.store.unsubscribe, chat_id)
            except Exception as exc:
                LOG.warning("Subscriber delivery not confirmed: %s", type(exc).__name__)

    def _signals(self):
        since = self.store.min_active_since(now=self.clock())
        if since is None:
            return []
        cutoff = max(0, since - 120, self.clock() - 7200)
        return sorted(
            main_signals(self.root / "data" / "bollinger-cycles.db", cutoff, include_cancelled=True)
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
            or not in_window(row["entry_at"], current_time)
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

            await asyncio.gather(
                *(self._deliver(bot, chat_id, joined_at, row, safe_text, content_sha)
                  for chat_id, joined_at in subscribers)
            )
            if send_timeout(row["entry_at"], self.clock()) <= 0:
                break

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
                        not self.store.allows_timeframe(chat_id, row["seconds"])
                        or row["seconds"] not in SIGNAL_TIMEFRAMES
                    ):
                        continue
                    if (
                        (self.require_sessions and not self.store.session_allows_signal(
                            chat_id, row["sent_at"], current_time
                        ))
                        or row["sent_at"] < joined_at
                        or row["sent_at"] > current_time
                        or current_time - row["sent_at"] > MAX_SEND_AGE_SECONDS
                        or row["entry_at"] - current_time < MIN_ENTRY_LEAD_SECONDS
                    ):
                        continue
                    await self._deliver(
                        bot, chat_id, joined_at, row, safe_text, content_sha
                    )

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

    async def notify_session_reminders(self, bot):
        """Only one opt-in renewal reminder for an active hourly session."""
        if not self.require_sessions:
            return
        due = await asyncio.to_thread(self.store.session_reminders_due, self.clock())
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Я на связи — продлить", callback_data="c:session:start")
        ]])
        for chat_id, expires in due:
            try:
                await bot.send_message(
                    chat_id,
                    "⏳ Сеанс сигналов скоро завершится. "
                    "Нажми кнопку, чтобы получать новые сигналы ещё до часа.",
                    reply_markup=keyboard, parse_mode=None,
                )
                await asyncio.to_thread(
                    self.store.mark_session_reminded, chat_id, expires, self.clock()
                )
            except TelegramForbiddenError:
                await asyncio.to_thread(self.store.unsubscribe, chat_id)
            except Exception as exc:
                LOG.warning("Subscriber session reminder failed: %s", type(exc).__name__)

    async def run(self, bot):
        while True:
            try:
                await self.update(bot)
                current = self.clock()
                if current - self._last_notice_check >= 60:
                    self._last_notice_check = current
                    await self.notify_expiry(bot)
                    await self.notify_session_reminders(bot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Subscriber relay failed: %s", type(exc).__name__)
            await asyncio.sleep(POLL_SECONDS)
