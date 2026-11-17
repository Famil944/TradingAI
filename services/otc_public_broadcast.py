"""Opt-in allowlisted public Telegram publisher. No virtual account reads or writes."""
import asyncio
import logging
import os
import sqlite3
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from services.otc_public_stats import (
    MAIN_LABEL, METHOD_LABELS, main_signals, metric_units, shadow_signals,
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
    for i, state in enumerate(row.get("attempts") or []):
        lines.append(
            f"{i+1}. {_ts(row['entry_at']+i*row['seconds'])[6:]} · {ICONS.get(state, '❔')}"
        )
    lines.append("Результат: " + ICONS.get(row["outcome"], "⏳") + " " + row["outcome"])
    lines.append("Источник: OTCharts · не подтверждено брокером")
    if row["origin"] == "shadow":
        lines.append("ИССЛЕДОВАНИЕ · не был отправлен как реальная торговая сделка")
    return "\n".join(lines)


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
    def __init__(self, root, channel_id):
        self.root = Path(root)
        self.channel_id = int(channel_id)
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
                    PRIMARY KEY(channel_id,signal_id)
                );
                CREATE TABLE IF NOT EXISTS sent_batches(
                    channel_id INTEGER NOT NULL,batch_key TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    PRIMARY KEY(channel_id,batch_key)
                );
            """)
            db.execute(
                "INSERT OR IGNORE INTO channels(channel_id,enabled_at) VALUES(?,?)",
                (self.channel_id, time.time()),
            )
            db.commit()
            self.enabled_at = db.execute(
                "SELECT enabled_at FROM channels WHERE channel_id=?", (self.channel_id,)
            ).fetchone()[0]
        self.shadow_enabled = set(
            (os.getenv("OTC_PUBLIC_SHADOW_METHODS") or "").replace(" ", "").split(",")
        ) & set(METHOD_LABELS)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def _snapshot(self):
        rows = main_signals(self.root / "data" / "bollinger-cycles.db", self.enabled_at)
        if self.shadow_enabled:
            rows.extend(
                row for row in shadow_signals(
                    self.root / "data" / "community-cycles.db", self.enabled_at
                )
                if row["strategy"] in self.shadow_enabled
            )
        return sorted(rows, key=lambda r: (r["created_at"], r["id"]))

    def _sent(self, identifier):
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT message_id,last_outcome FROM sent_signals "
                "WHERE channel_id=? AND signal_id=?",
                (self.channel_id, identifier),
            ).fetchone()

    def _record(self, identifier, message_id, outcome):
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO sent_signals(channel_id,signal_id,message_id,last_outcome)"
                " VALUES(?,?,?,?) ON CONFLICT(channel_id,signal_id) DO UPDATE SET"
                " message_id=excluded.message_id,last_outcome=excluded.last_outcome",
                (self.channel_id, identifier, message_id, outcome),
            )
            db.commit()

    def _has_batch(self, key):
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT 1 FROM sent_batches WHERE channel_id=? AND batch_key=?",
                (self.channel_id, key),
            ).fetchone() is not None

    def _record_batch(self, key, message_id):
        with closing(self._connect()) as db:
            db.execute(
                "INSERT OR IGNORE INTO sent_batches VALUES(?,?,?)",
                (self.channel_id, key, message_id),
            )
            db.commit()

    async def update(self, bot):
        rows = await asyncio.to_thread(self._snapshot)
        for row in rows:
            previous = await asyncio.to_thread(self._sent, row["id"])
            if previous is None:
                msg = await bot.send_message(
                    self.channel_id, public_text(row), parse_mode=None
                )
                await asyncio.to_thread(
                    self._record, row["id"], msg.message_id, row["outcome"]
                )
            elif previous[1] != row["outcome"]:
                await bot.edit_message_text(
                    public_text(row), chat_id=self.channel_id,
                    message_id=previous[0], parse_mode=None,
                )
                await asyncio.to_thread(
                    self._record, row["id"], previous[0], row["outcome"]
                )
        for metric in ("A", "B", "C"):
            units = metric_units(rows, metric)
            groups = defaultdict(list)
            for row in units:
                if row["outcome"] in ("WIN", "LOSS") and row["created_at"] >= self.enabled_at:
                    groups[(row["origin"], row["strategy"], row["version"], row["seconds"])].append(row)
            for key, group in groups.items():
                group.sort(key=lambda r: (r["resolved_at"], r["id"]))
                for start in range(0, len(group) - 9, 10):
                    ten = group[start:start + 10]
                    # Include version and immutable last ID; never republish this batch.
                    batch_key = ":".join(map(str, (metric,) + key + (ten[-1]["id"],)))
                    if await asyncio.to_thread(self._has_batch, batch_key):
                        continue
                    msg = await bot.send_message(
                        self.channel_id, batch_report(ten, metric, key[3]),
                        parse_mode=None,
                    )
                    await asyncio.to_thread(
                        self._record_batch, batch_key, msg.message_id
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
