"""Separate experimental variable-expiry forecasts, never mixed into ordinary statistics."""

import asyncio
import contextlib
import json
import logging
import math
import sqlite3
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from services.otc_confirmations import build

from .quality import expected_return

logger = logging.getLogger(__name__)
DURATIONS = (60, 120, 180, 300, 900)


class PreviewJournal:
    def __init__(self, path, label="XGBoost", per_horizon=False):
        self.path = Path(path)
        self.label = label
        self.per_horizon = per_horizon
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            db.execute("BEGIN IMMEDIATE")
            uniqueness = (
                "UNIQUE(entry_at,expires_at)" if per_horizon else "UNIQUE(entry_at)"
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS predictions(
                id TEXT PRIMARY KEY,symbol TEXT,created_at REAL,entry_at REAL,expires_at REAL,
                direction TEXT,payout REAL,model_sha TEXT,evidence TEXT,sent_at REAL,
                outcome TEXT DEFAULT 'PENDING',entry_price REAL,expiry_price REAL,
                """
                + uniqueness
                + ")"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(predictions)")}
            if "message_id" not in columns:
                db.execute("ALTER TABLE predictions ADD COLUMN message_id INTEGER")
            if "resolved_at" not in columns:
                db.execute("ALTER TABLE predictions ADD COLUMN resolved_at REAL")
            db.commit()

    def _blocked(self, db, now, seconds=None):
        scope, params = ("", ())
        if self.per_horizon:
            scope, params = " AND expires_at-entry_at=?", (seconds,)
        if db.execute(
            "SELECT 1 FROM predictions WHERE outcome='PENDING'" + scope + " LIMIT 1",
            params,
        ).fetchone():
            return "pending_position"
        latest = db.execute(
            "SELECT max(resolved_at) FROM predictions WHERE sent_at IS NOT NULL "
            "AND outcome IN ('WIN','LOSS','DRAW','UNKNOWN')" + scope,
            params,
        ).fetchone()[0]
        if latest is not None and now < latest + 60:
            return "result_cooldown"
        return None

    def blocked(self, now, seconds=None):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            return self._blocked(db, now, seconds)

    def reserve(self, selected, now):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute("BEGIN IMMEDIATE")
            if self._blocked(db, now, selected["expires_at"] - selected["entry_at"]):
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

    def finish(
        self,
        identifier,
        outcome,
        entry=None,
        expiry=None,
        sent_at=None,
        *,
        resolved_at=None,
    ):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute(
                "UPDATE predictions SET outcome=?,entry_price=?,expiry_price=?,sent_at=COALESCE(?,sent_at),resolved_at=? WHERE id=? AND outcome='PENDING'",
                (
                    outcome,
                    entry,
                    expiry,
                    sent_at,
                    time.time() if resolved_at is None else resolved_at,
                    identifier,
                ),
            )
            db.commit()

    def sent(self, identifier, now, message_id=None, late=False):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute(
                """UPDATE predictions SET sent_at=?,message_id=?,
                outcome=CASE WHEN ? AND outcome='PENDING' THEN 'LATE' ELSE outcome END
                WHERE id=?""",
                (now, message_id, late, identifier),
            )
            db.commit()

    def cards_to_update(self):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            return db.execute(
                """SELECT id,message_id,evidence,outcome FROM predictions
                WHERE message_id IS NOT NULL AND outcome!='PENDING'
                ORDER BY expires_at LIMIT 10"""
            ).fetchall()

    def card_updated(self, identifier):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute(
                "UPDATE predictions SET message_id=NULL WHERE id=?", (identifier,)
            )
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

    def stats(self, seconds=None, since=0):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            rows = db.execute(
                "SELECT outcome,count(*) FROM predictions WHERE (? IS NULL OR expires_at-entry_at=?) AND entry_at>=? GROUP BY outcome",
                (seconds, seconds, since),
            ).fetchall()
        counts = dict(rows)
        wins = counts.get("WIN", 0)
        losses = counts.get("LOSS", 0)
        rate = f"{100 * wins / (wins + losses):.1f}%" if wins + losses else "—"
        title = (
            (
                "🟣 "
                if self.label == "Groq"
                else "🔵 "
                if self.label.startswith("Основной")
                else "🟠 "
            )
            + self.label
            + " · отдельный тест"
            + (f" · {seconds // 60} мин" if seconds else "")
        )
        return (
            f"{title}\nВин {wins} (+)\nЛос {losses} (−)\nПропуск {sum(counts.get(k, 0) for k in ('CANCELLED', 'NOT_DELIVERED', 'LATE'))}\nВинрейт {rate}"
            + (f"\nНет данных {counts['UNKNOWN']}" if counts.get("UNKNOWN") else "")
            + (f"\nОжидают {counts['PENDING']}" if counts.get("PENDING") else "")
        )


def select_forecast(
    runtime, histories, assets, quotes, now, session_end=None, reasons=None
):
    entry = int(now) // 60 * 60 + 60
    if not 15 <= entry - now <= 23:
        return None
    if reasons is None:
        reasons = Counter()
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
            reasons["asset_or_quote_ineligible"] += 1
            continue
        for seconds in DURATIONS:
            # M5/M15 only on their actual next candle-open boundary; using
            # the current unfinished higher-timeframe candle would leak data.
            if seconds in (300, 900) and entry % seconds != 0:
                reasons["not_timeframe_boundary"] += 1
                continue
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
                reasons[result.get("status", "no_prediction")] += 1
                continue
            direction = "UP" if probabilities[2] >= probabilities[0] else "DOWN"
            ev = expected_return(probabilities, direction, asset.payout)
            if ev is None:
                reasons["invalid_model_probabilities"] += 1
                continue
            confirmations = build(bars, direction, seconds, now)
            if not confirmations["available"] or confirmations["count"] < 1:
                reasons["no_directional_confirmation"] += 1
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
                selection_policy="xgboost-one-confirmation-v1",
                evidence=dict(confirmations=confirmations),
                confirmations=confirmations,
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


def signal_card(selected, outcome=None, *, source):
    from services.otc_regime import describe

    arrow = "🟢⬆️" if selected["direction"] == "UP" else "🔴⬇️"
    status = {
        "WIN": "✅",
        "LOSS": "❌",
        "DRAW": "➖",
        "UNKNOWN": "❔",
        "CANCELLED": "Отменён",
        "LATE": "Опоздал",
    }.get(outcome)
    report = selected.get("confirmations") or {}
    count = (
        f"{report['count']}/{report.get('total', 5)}"
        if report.get("available")
        else "—/5"
    )
    state = report.get("market_state")
    market = (
        f"\nРынок M{report.get('period', 60) // 60}: {describe(state)}" if state else ""
    )
    minutes = int(selected["expires_at"] - selected["entry_at"]) // 60
    clock = datetime.fromtimestamp(
        selected["entry_at"], ZoneInfo("Europe/Moscow")
    ).strftime("%H:%M:%S")
    heading = f"СИГНАЛ {arrow}" + (f" {status}" if status else "")
    return (
        f"{heading}\n\nИсточник: {source}\nАктив: {selected['name']}\nИндикаторы: {count}"
        f"{market}\nЭкспирация: M{minutes}\nВход: {clock} МСК"
    )


class MLPreview:
    icon = "🟠"
    label = "XGBoost"
    # The last closed minute is already available here. Start inference
    # before the delivery window so a cold model load can finish in time.
    planning_window = (20, 23)

    def __init__(self, journal, runtime, clock=time.time, settlement_provider=None):
        self.journal, self.runtime, self.clock = journal, runtime, clock
        self.settlement_provider = settlement_provider
        self.settlement_cache = {}
        self.last_settlement_fetch = {}
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

    async def settle_pending(self, history):
        for row in await asyncio.to_thread(self.journal.pending):
            now = self.clock()
            bars = history.get(row["symbol"], [])
            if self.settlement_provider is not None and row.get("sent_at") is not None:
                if now < row["expires_at"] + 60:
                    continue
                symbol = row["symbol"]
                if now - self.last_settlement_fetch.get(symbol, float("-inf")) >= 60:
                    self.last_settlement_fetch[symbol] = now
                    try:
                        quota = await self.settlement_provider.usage()
                        if quota.remaining <= 10:
                            raise RuntimeError("quota reserve")
                        self.settlement_cache[
                            symbol
                        ] = await self.settlement_provider.candles(symbol, now)
                    except Exception as exc:
                        logger.warning(
                            "XGBoost settlement history: %s", type(exc).__name__
                        )
                # Never substitute provisional tick opens for canonical history.
                bars = self.settlement_cache.get(symbol, [])
            result = settle_preview(row, bars, self.clock())
            if result:
                await asyncio.to_thread(self.journal.finish, row["id"], *result)

    async def plan(self, bot, chat_id, snapshot):
        self.snapshot = snapshot
        if await asyncio.to_thread(self.journal.blocked, self.clock()):
            return
        history, assets, quotes, end = snapshot
        histories = {symbol: list(bars) for symbol, bars in history.items()}
        reasons = Counter()
        selected = await asyncio.to_thread(
            select_forecast,
            self.runtime,
            histories,
            dict(assets),
            dict(quotes),
            self.clock(),
            end,
            reasons,
        )
        if not selected:
            logger.info("XGBoost skipped: %s", dict(reasons) or "no candidates")
            return
        delay = selected["entry_at"] - self.clock() - 20
        if delay > 0:
            await asyncio.sleep(delay)
        if not self.enabled:
            return
        if not 15 <= selected["entry_at"] - self.clock() <= 20:
            logger.info("XGBoost skipped: inference missed delivery window")
            return
        if self.snapshot:
            _, assets, quotes, _ = self.snapshot
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
            logger.info("XGBoost skipped: asset or quote changed before delivery")
            return
        selected["payout"] = current.payout
        selected["estimated_ev"] = expected_return(
            selected["probabilities"], selected["direction"], current.payout
        )
        if selected["estimated_ev"] is None:
            logger.info("XGBoost skipped: invalid probabilities at delivery")
            return
        await self.publish(selected, bot, chat_id)

    async def publish(self, selected, bot, chat_id):
        from services.otc_confirmations import build
        from services.otc_delivery_guard import delivery_snapshot

        selected = dict(selected)
        final_check = delivery_snapshot(selected, self.snapshot, self.clock())
        if final_check is None:
            logger.info("%s skipped at final quote/latency check", self.label)
            return False
        selected["timing"] = final_check
        evidence = selected.get("evidence") or {}
        confirmations = evidence.get("confirmations")
        if confirmations is None:
            history = self.snapshot[0] if self.snapshot else {}
            # A Groq decision uses its original packet cutoff, before its wait.
            asof = selected.get("feature_asof", self.clock())
            confirmations = build(
                list(history.get(selected["symbol"], [])),
                selected["direction"],
                int(selected["expires_at"] - selected["entry_at"]),
                asof,
            )
        selected["confirmations"] = confirmations
        identifier = await asyncio.to_thread(
            self.journal.reserve, selected, self.clock()
        )
        if not identifier:
            return
        if not 15 <= selected["entry_at"] - self.clock() <= 20:
            await asyncio.to_thread(self.journal.finish, identifier, "LATE")
            return
        from services.otc_delivery_guard import claim

        if not await asyncio.to_thread(
            claim,
            self.journal.path.with_name("signal-delivery.db"),
            selected,
            self.label,
            self.clock(),
        ):
            await asyncio.to_thread(self.journal.finish, identifier, "CANCELLED")
            logger.info("%s duplicate suppressed: %s", self.label, selected["symbol"])
            return
        if not 15 <= selected["entry_at"] - self.clock() <= 20:
            await asyncio.to_thread(self.journal.finish, identifier, "LATE")
            return
        minutes = int(selected["expires_at"] - selected["entry_at"]) // 60
        try:
            timeout = min(3, max(0.1, selected["entry_at"] - 15 - self.clock()))
            async with asyncio.timeout(timeout):
                message = await bot.send_message(
                    chat_id, signal_card(selected, source=self.label)
                )
        except (Exception, asyncio.CancelledError):
            await asyncio.to_thread(self.journal.finish, identifier, "NOT_DELIVERED")
            return
        now = self.clock()
        message_id = getattr(message, "message_id", None)
        late = not 15 <= selected["entry_at"] - now <= 20
        await asyncio.to_thread(
            self.journal.sent,
            identifier,
            now,
            message_id if isinstance(message_id, int) else None,
            late,
        )
        if not late:
            logger.info(
                "%s preview sent: %s, %d minutes, %.1fs before entry",
                self.label,
                selected["symbol"],
                minutes,
                selected["entry_at"] - now,
            )
            return True

    async def update_cards(self, bot, chat_id):
        from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

        for identifier, message_id, evidence, outcome in await asyncio.to_thread(
            self.journal.cards_to_update
        ):
            try:
                async with asyncio.timeout(3):
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=signal_card(
                            json.loads(evidence), outcome, source=self.label
                        ),
                    )
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                logger.info("Signal card edit rejected: %s", type(exc).__name__)
            except Exception as exc:
                logger.warning("Signal card edit retry: %s", type(exc).__name__)
                break
            await asyncio.to_thread(self.journal.card_updated, identifier)

    async def run(self, bot, chat_id):
        next_card_update = 0
        try:
            while True:
                try:
                    if self.clock() >= next_card_update:
                        next_card_update = self.clock() + 10
                        await self.update_cards(bot, chat_id)
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
                        await self.settle_pending(history)
                        now = self.clock()
                        entry = int(now) // 60 * 60 + 60
                        if (
                            self.enabled
                            and self.planning_window[0]
                            <= entry - now
                            <= self.planning_window[1]
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
