"""Opt-in allowlisted public Telegram publisher. No virtual account reads or writes."""
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from services.otc_public_stats import (
    METHOD_LABELS, _attempts, main_signals, metric_units, shadow_signals,
)

LOG = logging.getLogger(__name__)
MSK = ZoneInfo("Europe/Moscow")
ICONS = {"WIN": "✅", "LOSS": "❌", "DRAW": "➖", "UNKNOWN": "❔", "PENDING": "⏳"}


def _ts(at):
    return datetime.fromtimestamp(at, MSK).strftime("%d.%m %H:%M")


def public_text(row):
    """Only explicit safe fields; never forward or modify private Telegram cards."""
    heading = row["label"]
    arrow = "🟢⬆️" if row["direction"] == "UP" else "🔴⬇️"
    lines = [
        f"{heading} · M{row['seconds'] // 60} {arrow}",
        row["symbol"], "Прогноз · OTC · " + _ts(row["entry_at"]) + " МСК",
    ]
    attempts = row.get("attempts") or []
    stopped = False
    for i in range(3):
        state = attempts[i] if i < len(attempts) else None
        marker = (
            ICONS.get(state, "❔") if state is not None
            else "— остановлено" if stopped else "⏳"
            if row["outcome"] == "PENDING" else "—"
        )
        lines.append(
            f"{i+1}. {_ts(row['entry_at']+i*row['seconds'])[6:]} · {marker}"
        )
        if state in ("WIN", "DRAW", "UNKNOWN"):
            stopped = True
    lines.append("Результат: " + ICONS.get(row["outcome"], "⏳") + " " + row["outcome"])
    lines.append("Источник: OTCharts · не подтверждено брокером")
    if row["origin"] == "shadow":
        lines.append("ИССЛЕДОВАНИЕ · не был отправлен как реальная торговая сделка")
    return "\n".join(lines)


def safe_card(row, live_cycle=None):
    """Use public fields plus in-memory attempt outcomes; never copy wallet data."""
    if live_cycle is not None and row["origin"] == "main":
        cache = getattr(live_cycle, "card_cache", {})
        states = cache.get(row["id"].removeprefix("main:"))
        if isinstance(states, list):
            row = dict(row, attempts=_attempts(states))
    return public_text(row)


def batch_report(rows, metric, seconds):
    wins = sum(r["outcome"] == "WIN" for r in rows)
    losses = len(rows) - wins
    title = {"A": "ПОПЫТКИ", "B": "СИГНАЛЫ ×3", "C": "ЦИКЛЫ ×6"}[metric]
    lines = [
        f"📊 {rows[0]['label']} · {title} · M{seconds // 60}",
        f"Последние 10 подтверждённых исходов · {_ts(rows[0]['resolved_at'])}–{_ts(rows[-1]['resolved_at'])} МСК",
    ]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {row['symbol']} — {ICONS[row['outcome']]}")
    lines.extend([
        f"✅ WIN {wins} · ❌ LOSS {losses} · WR {100*wins/len(rows):.1f}%",
        "Только исходы прогнозов OTCharts, не прибыль и не сделки брокера.",
    ])
    if rows[0]["origin"] == "shadow":
        lines.append("ФОНОВЫЙ ТЕСТ · гипотезы без отправленных входов")
    return "\n".join(lines)


class PublicPublisher:
    """Separate journal records what was ACKed by Telegram, not an exactly-once guarantee."""
    def __init__(self, root, channel_id, live_cycle=None):
        self.root = Path(root)
        self.live_cycle = live_cycle
        self.channel_id = int(channel_id)
        if not str(self.channel_id).startswith("-100"):
            raise ValueError("OTC_PUBLIC_CHANNEL_ID must be a Telegram channel ID (-100...)")
        self.path = self.root / "data" / "public-telegram.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS channels(
                    channel_id INTEGER PRIMARY KEY,enabled_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sent_signals(
                    channel_id INTEGER NOT NULL,signal_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,last_outcome TEXT NOT NULL,
                    last_content_sha TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(channel_id,signal_id)
                );
                CREATE TABLE IF NOT EXISTS sent_batches(
                    channel_id INTEGER NOT NULL,batch_key TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    PRIMARY KEY(channel_id,batch_key)
                );
                CREATE TABLE IF NOT EXISTS shadow_flags(
                    channel_id INTEGER NOT NULL,method TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    enabled_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(channel_id,method)
                );
                CREATE TABLE IF NOT EXISTS report_units(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL,
                    group_key TEXT NOT NULL,
                    unit_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    batch_key TEXT,
                    UNIQUE(channel_id,group_key,unit_id)
                );
                CREATE INDEX IF NOT EXISTS report_units_pending
                    ON report_units(channel_id,group_key,batch_key,id);
            """)
            # Additive migration for channels created by the previous preview version.
            columns = {row[1] for row in db.execute("PRAGMA table_info(shadow_flags)")}
            if "enabled_at" not in columns:
                db.execute(
                    "ALTER TABLE shadow_flags ADD COLUMN enabled_at REAL NOT NULL DEFAULT 0"
                )
            delivery_columns = {
                row[1] for row in db.execute("PRAGMA table_info(sent_signals)")
            }
            if "last_content_sha" not in delivery_columns:
                db.execute(
                    "ALTER TABLE sent_signals ADD COLUMN "
                    "last_content_sha TEXT NOT NULL DEFAULT ''"
                )
            db.execute(
                "INSERT OR IGNORE INTO channels(channel_id,enabled_at) VALUES(?,?)",
                (self.channel_id, time.time()),
            )
            db.commit()
            self.enabled_at = db.execute(
                "SELECT enabled_at FROM channels WHERE channel_id=?", (self.channel_id,)
            ).fetchone()[0]
        defaults = set(
            (os.getenv("OTC_PUBLIC_SHADOW_METHODS") or "").replace(" ", "").split(",")
        ) & set(METHOD_LABELS)
        with closing(self._connect()) as db:
            for method in METHOD_LABELS:
                db.execute(
                    "INSERT OR IGNORE INTO shadow_flags"
                    "(channel_id,method,enabled,enabled_at) VALUES(?,?,?,?)",
                    (self.channel_id, method, int(method in defaults), 
                     self.enabled_at if method in defaults else 0),
                )
            db.commit()

    def flags(self):
        with closing(self._connect()) as db:
            return dict(db.execute(
                "SELECT method,enabled FROM shadow_flags WHERE channel_id=?",
                (self.channel_id,),
            ).fetchall())

    def _flag_windows(self):
        with closing(self._connect()) as db:
            return {
                name: float(at)
                for name, enabled, at in db.execute(
                    "SELECT method,enabled,enabled_at FROM shadow_flags "
                    "WHERE channel_id=?", (self.channel_id,)
                ).fetchall()
                if enabled
            }

    def toggle_shadow(self, method):
        if method not in METHOD_LABELS:
            raise ValueError("Unknown background strategy")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT enabled FROM shadow_flags WHERE channel_id=? AND method=?",
                (self.channel_id, method),
            ).fetchone()
            if row is None:
                raise ValueError("Missing strategy switch")
            new_status = not bool(row[0])
            db.execute(
                "UPDATE shadow_flags SET enabled=?,enabled_at=? "
                "WHERE channel_id=? AND method=?",
                (int(new_status), time.time() if new_status else 0,
                 self.channel_id, method),
            )
            # A paused channel must not later publish a stale or mixed batch.
            for (key,) in db.execute(
                "SELECT DISTINCT group_key FROM report_units "
                "WHERE channel_id=? AND batch_key IS NULL",
                (self.channel_id,),
            ).fetchall():
                if json.loads(key)[2] == method:
                    db.execute(
                        "DELETE FROM report_units WHERE channel_id=? "
                        "AND group_key=? AND batch_key IS NULL",
                        (self.channel_id, key),
                    )
            db.commit()
        return new_status

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def _snapshot(self):
        rows = main_signals(self.root / "data" / "bollinger-cycles.db", self.enabled_at)
        windows = self._flag_windows()
        if windows:
            oldest = min(max(self.enabled_at, t) for t in windows.values())
            rows.extend(
                row for row in shadow_signals(
                    self.root / "data" / "community-cycles.db", oldest
                )
                if row["strategy"] in windows
                and row["created_at"] >= max(self.enabled_at, windows[row["strategy"]])
            )
        return sorted(rows, key=lambda r: (r["created_at"], r["id"]))

    def _sent(self, identifier):
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT message_id,last_outcome,last_content_sha FROM sent_signals "
                "WHERE channel_id=? AND signal_id=?",
                (self.channel_id, identifier),
            ).fetchone()

    def _record(self, identifier, message_id, outcome, content_sha):
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO sent_signals"
                "(channel_id,signal_id,message_id,last_outcome,last_content_sha)"
                " VALUES(?,?,?,?,?) ON CONFLICT(channel_id,signal_id) DO UPDATE SET"
                " message_id=excluded.message_id,last_outcome=excluded.last_outcome,"
                "last_content_sha=excluded.last_content_sha",
                (self.channel_id, identifier, message_id, outcome, content_sha),
            )
            db.commit()

    def _enqueue_units(self, rows):
        # Queue only newly confirmed outcomes. Immutable IDs prevent a late
        # UNKNOWN->WIN reconciliation from shifting previously sent batches.
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            for metric in ("A", "B", "C"):
                for row in metric_units(rows, metric):
                    if (row["outcome"] not in ("WIN", "LOSS")
                            or row["created_at"] < self.enabled_at):
                        continue
                    key = json.dumps([
                        metric, row["origin"], row["strategy"], row["version"],
                        row["seconds"],
                    ], separators=(",", ":"))
                    payload = {
                        name: row[name] for name in (
                            "id", "origin", "strategy", "version", "seconds",
                            "label", "symbol", "created_at", "resolved_at", "outcome",
                        )
                    }
                    db.execute(
                        "INSERT OR IGNORE INTO report_units"
                        "(channel_id,group_key,unit_id,payload,batch_key)"
                        " VALUES(?,?,?,? ,NULL)",
                        (self.channel_id, key, row["id"],
                         json.dumps(payload, ensure_ascii=False)),
                    )
            db.commit()

    def _pending_batches(self):
        with closing(self._connect()) as db:
            keys = [
                r[0] for r in db.execute(
                    "SELECT DISTINCT group_key FROM report_units "
                    "WHERE channel_id=? AND batch_key IS NULL",
                    (self.channel_id,),
                ).fetchall()
            ]
            active = self._flag_windows()
            pending = []
            for key in keys:
                metric, origin, method, version, seconds = json.loads(key)
                if origin == "shadow" and method not in active:
                    continue
                group = db.execute(
                    "SELECT id,unit_id,payload FROM report_units "
                    "WHERE channel_id=? AND group_key=? AND batch_key IS NULL "
                    "ORDER BY id LIMIT 10",
                    (self.channel_id, key),
                ).fetchall()
                if len(group) == 10:
                    unit_ids = [r[1] for r in group]
                    batch_key = hashlib.sha256(
                        json.dumps([key, unit_ids], separators=(",", ":")).encode()
                    ).hexdigest()
                    payload = [json.loads(r[2]) for r in group]
                    pending.append((key, batch_key, [r[0] for r in group],
                                    metric, seconds, payload))
            return pending

    def _commit_batch(self, batch_key, identifiers, message_id):
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO sent_batches(channel_id,batch_key,message_id)"
                " VALUES(?,?,?)",
                (self.channel_id, batch_key, message_id),
            )
            db.executemany(
                "UPDATE report_units SET batch_key=? "
                "WHERE channel_id=? AND id=? AND batch_key IS NULL",
                [(batch_key, self.channel_id, key) for key in identifiers],
            )
            db.commit()

    async def update(self, bot):
        rows = await asyncio.to_thread(self._snapshot)
        for row in rows:
            text = safe_card(row, self.live_cycle)
            content_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            previous = await asyncio.to_thread(self._sent, row["id"])
            if previous is None:
                if row["entry_at"] <= time.time() + 3:
                    # Never present historical or late entries as actionable signals.
                    continue
                msg = await bot.send_message(
                    self.channel_id, text, parse_mode=None
                )
                await asyncio.to_thread(
                    self._record, row["id"], msg.message_id,
                    row["outcome"], content_sha
                )
            elif previous[1] != row["outcome"] or previous[2] != content_sha:
                await bot.edit_message_text(
                    text, chat_id=self.channel_id,
                    message_id=previous[0], parse_mode=None,
                )
                await asyncio.to_thread(
                    self._record, row["id"], previous[0],
                    row["outcome"], content_sha
                )
        await asyncio.to_thread(self._enqueue_units, rows)
        while True:
            batches = await asyncio.to_thread(self._pending_batches)
            if not batches:
                break
            for key, batch_key, ids, metric, seconds, payload in batches:
                msg = await bot.send_message(
                    self.channel_id, batch_report(payload, metric, seconds),
                    parse_mode=None,
                )
                await asyncio.to_thread(
                    self._commit_batch, batch_key, ids, msg.message_id
                )

    async def run(self, bot):
        while True:
            try:
                await self.update(bot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Public publisher skipped tick: %s", type(exc).__name__)
            await asyncio.sleep(12)
