"""Separate experimental variable-expiry forecasts, never mixed into ordinary statistics."""

import asyncio
import contextlib
import json
import logging
import math
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .quality import expected_return

logger = logging.getLogger(__name__)
DURATIONS = (60, 120, 180, 300, 900)


class PreviewJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS predictions(
                id TEXT PRIMARY KEY,symbol TEXT,created_at REAL,entry_at REAL,expires_at REAL,
                direction TEXT,payout REAL,model_sha TEXT,evidence TEXT,sent_at REAL,
                outcome TEXT DEFAULT 'PENDING',entry_price REAL,expiry_price REAL,
                UNIQUE(entry_at))""")
            db.commit()

    def reserve(self, selected, now):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM predictions WHERE outcome='PENDING' LIMIT 1"
            ).fetchone():
                return None
            identifier = uuid.uuid4().hex
            try:
                db.execute(
                    "INSERT INTO predictions(id,symbol,created_at,entry_at,expires_at,direction,payout,model_sha,evidence) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        selected["symbol"],
                        now,
                        selected["entry_at"],
                        selected["expires_at"],
                        selected["direction"],
                        selected["payout"],
                        selected["model_sha256"],
                        json.dumps(selected, allow_nan=False),
                    ),
                )
                db.commit()
                return identifier
            except sqlite3.IntegrityError:
                return None

    def finish(self, identifier, outcome, entry=None, expiry=None, sent_at=None):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute(
                "UPDATE predictions SET outcome=?,entry_price=?,expiry_price=?,sent_at=COALESCE(?,sent_at) WHERE id=? AND outcome='PENDING'",
                (outcome, entry, expiry, sent_at, identifier),
            )
            db.commit()

    def sent(self, identifier, now):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute("UPDATE predictions SET sent_at=? WHERE id=?", (now, identifier))
            db.commit()

    def pending(self):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(r)
                for r in db.execute("SELECT * FROM predictions WHERE outcome='PENDING'")
            ]

    def cancel_future(self, now):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute(
                "UPDATE predictions SET outcome='CANCELLED' WHERE entry_at>? AND outcome='PENDING'",
                (now,),
            )
            db.commit()

    def stats(self, seconds=None):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            rows = db.execute(
                "SELECT outcome,count(*) FROM predictions WHERE (? IS NULL OR expires_at-entry_at=?) GROUP BY outcome",
                (seconds, seconds),
            ).fetchall()
        counts = dict(rows)
        wins = counts.get("WIN", 0)
        losses = counts.get("LOSS", 0)
        rate = f"{100 * wins / (wins + losses):.1f}%" if wins + losses else "—"
        title = "XGBoost · отдельный тест" + (
            f" · {seconds // 60} мин" if seconds else ""
        )
        return (
            f"{title}\nВин {wins}\nЛос {losses}\nПропуск {sum(counts.get(k, 0) for k in ('CANCELLED', 'NOT_DELIVERED', 'LATE'))}\nВинрейт {rate}"
            + (f"\nНет данных {counts['UNKNOWN']}" if counts.get("UNKNOWN") else "")
            + (f"\nОжидают {counts['PENDING']}" if counts.get("PENDING") else "")
        )


def select_forecast(runtime, histories, assets, quotes, now, session_end=None):
    entry = int(now) // 60 * 60 + 60
    if not 15 <= entry - now <= 20:
        return None
    best = None
    for symbol, bars in histories.items():
        asset = assets.get(symbol)
        if (
            not asset
            or asset.open is not True
            or asset.payout is None
            or not math.isfinite(asset.payout)
            or not 90 <= asset.payout <= 100
            or asset.observed_at is None
            or not 0 <= now - asset.observed_at <= 60
            or not 0 <= now - quotes.get(symbol, float("-inf")) <= 8
        ):
            continue
        for seconds in DURATIONS:
            if session_end and entry + seconds + 60 > session_end:
                continue
            row = dict(
                entry_at=entry,
                expires_at=entry + seconds,
                direction="UP",
                feature_asof=now,
                decision_lead_seconds=20,
            )
            result = runtime.inspect(row, bars, asset.payout)
            probabilities = result.get("probabilities")
            if probabilities is None:
                continue
            direction = "UP" if probabilities[2] >= probabilities[0] else "DOWN"
            probability = probabilities[2 if direction == "UP" else 0]
            ev = expected_return(probabilities, direction, asset.payout)
            # Same numerical selection thresholds; this research forecast remains explicitly unqualified.
            if probability < 0.60 or ev is None or ev < 0.05:
                continue
            candidate = dict(
                symbol=symbol,
                name=asset.name or symbol,
                entry_at=entry,
                expires_at=entry + seconds,
                direction=direction,
                payout=asset.payout,
                model_sha256=result["model_sha256"],
                model_role="unqualified_experimental",
                estimated_ev=ev,
                probabilities=probabilities,
                feature_asof=now,
                decision_lead_seconds=20,
            )
            if best is None or (ev, -seconds, symbol) > (
                best["estimated_ev"],
                -int(best["expires_at"] - entry),
                best["symbol"],
            ):
                best = candidate
    return best


def settle_preview(row, bars, now):
    if row.get("sent_at") is None and now >= row["entry_at"]:
        return "NOT_DELIVERED", None, None
    if now < row["expires_at"] + 60:
        return None
    by_time = {b.time: b for b in bars if b.time + 60 <= now}
    entry, expiry = (
        by_time.get(int(row["entry_at"])),
        by_time.get(int(row["expires_at"])),
    )
    if entry is None or expiry is None:
        return ("UNKNOWN", None, None) if now >= row["expires_at"] + 180 else None
    if not all(
        math.isfinite(value) and value > 0 for value in (entry.open, expiry.open)
    ):
        return "UNKNOWN", None, None
    delta = expiry.open - entry.open
    outcome = (
        "DRAW"
        if delta == 0
        else "WIN"
        if (delta > 0) == (row["direction"] == "UP")
        else "LOSS"
    )
    return outcome, entry.open, expiry.open


class MLPreview:
    def __init__(self, journal, runtime, clock=time.time):
        self.journal, self.runtime, self.clock = journal, runtime, clock
        self.enabled = True
        self.snapshot = None
        self.attempted = None
        self.planning = None

    def observe(self, history, assets, quotes, session_end):
        self.snapshot = (history, assets, quotes, session_end)

    async def stop(self):
        self.enabled = False
        if self.planning and not self.planning.done():
            self.planning.cancel()
            await asyncio.gather(self.planning, return_exceptions=True)
        await asyncio.to_thread(self.journal.cancel_future, self.clock())

    async def plan(self, bot, chat_id, snapshot):
        history, assets, quotes, end = snapshot
        histories = {symbol: list(bars) for symbol, bars in history.items()}
        selected = await asyncio.to_thread(
            select_forecast,
            self.runtime,
            histories,
            dict(assets),
            dict(quotes),
            self.clock(),
            end,
        )
        if (
            not selected
            or not self.enabled
            or not 15 <= selected["entry_at"] - self.clock() <= 20
        ):
            return
        current = assets.get(selected["symbol"])
        now = self.clock()
        if (
            current is None
            or current.open is not True
            or current.payout is None
            or not 90 <= current.payout <= 100
            or current.observed_at is None
            or not 0 <= now - current.observed_at <= 60
            or not 0 <= now - quotes.get(selected["symbol"], float("-inf")) <= 8
        ):
            return
        selected["payout"] = current.payout
        selected["estimated_ev"] = expected_return(
            selected["probabilities"], selected["direction"], current.payout
        )
        if selected["estimated_ev"] is None or selected["estimated_ev"] < 0.05:
            return
        identifier = await asyncio.to_thread(
            self.journal.reserve, selected, self.clock()
        )
        if not identifier:
            return
        if not 15 <= selected["entry_at"] - self.clock() <= 20:
            await asyncio.to_thread(self.journal.finish, identifier, "LATE")
            return
        minutes = int(selected["expires_at"] - selected["entry_at"]) // 60
        direction = "ВВЕРХ" if selected["direction"] == "UP" else "ВНИЗ"
        clock = datetime.fromtimestamp(
            selected["entry_at"], ZoneInfo("Europe/Moscow")
        ).strftime("%H:%M:%S")
        text = f"🧪 XGBoost · отдельный тест\n{selected['name']}\n{direction} · {minutes} мин\nСледующая минута · {clock} МСК. Опоздал — пропуск."
        try:
            timeout = min(3, max(0.1, selected["entry_at"] - 15 - self.clock()))
            async with asyncio.timeout(timeout):
                await bot.send_message(chat_id, text)
        except (Exception, asyncio.CancelledError):
            await asyncio.to_thread(self.journal.finish, identifier, "NOT_DELIVERED")
            return
        now = self.clock()
        if not 15 <= selected["entry_at"] - now <= 20:
            await asyncio.to_thread(
                self.journal.finish, identifier, "LATE", None, None, now
            )
        else:
            await asyncio.to_thread(self.journal.sent, identifier, now)
            logger.info(
                "XGBoost preview sent: %s, %d minutes, %.1fs before entry",
                selected["symbol"],
                minutes,
                selected["entry_at"] - now,
            )

    async def run(self, bot, chat_id):
        try:
            while True:
                try:
                    if self.planning and self.planning.done():
                        if not self.planning.cancelled():
                            error = self.planning.exception()
                            if error:
                                logger.warning(
                                    "XGBoost preview prediction: %s",
                                    type(error).__name__,
                                )
                        self.planning = None
                    if self.snapshot:
                        history, assets, quotes, end = self.snapshot
                        for row in await asyncio.to_thread(self.journal.pending):
                            result = settle_preview(
                                row, history.get(row["symbol"], []), self.clock()
                            )
                            if result:
                                await asyncio.to_thread(
                                    self.journal.finish, row["id"], *result
                                )
                        now = self.clock()
                        entry = int(now) // 60 * 60 + 60
                        if (
                            self.enabled
                            and 15 <= entry - now <= 20
                            and self.attempted != entry
                            and (self.planning is None or self.planning.done())
                        ):
                            self.attempted = entry
                            self.planning = asyncio.create_task(
                                self.plan(bot, chat_id, self.snapshot)
                            )
                except Exception as exc:
                    logger.warning("XGBoost preview loop: %s", type(exc).__name__)
                await asyncio.sleep(0.5)
        finally:
            await self.stop()
