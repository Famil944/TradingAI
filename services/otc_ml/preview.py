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
DURATIONS = (120, 300, 900)
MIN_MODEL_EV = 0.02


def aligned_entry(entry, seconds):
    """M5/M15 enter only when their corresponding candle closes."""
    return seconds not in (300, 900) or entry % seconds == 0


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

    def loss_cooldowns(self, now):
        # All horizons of this source share a symbol pause. A later WIN must
        # not erase a still-active LOSS pause; other sources remain independent.
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            return dict(
                db.execute(
                    "SELECT symbol, max(resolved_at)+300 FROM predictions "
                    "WHERE sent_at IS NOT NULL AND outcome='LOSS' "
                    "AND resolved_at>? GROUP BY symbol",
                    (now - 300,),
                ).fetchall()
            )

    def reserve(self, selected, now):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute("BEGIN IMMEDIATE")
            if self._blocked(db, now, selected["expires_at"] - selected["entry_at"]):
                return None
            if not aligned_entry(
                selected["entry_at"], selected["expires_at"] - selected["entry_at"]
            ):
                return None
            if db.execute(
                "SELECT 1 FROM predictions WHERE symbol=? AND outcome='LOSS' "
                "AND sent_at IS NOT NULL AND resolved_at>? LIMIT 1",
                (selected["symbol"], now - 300),
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

    def cancel_future(self, now, *, preserve_sent=False):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute(
                "UPDATE predictions SET outcome='CANCELLED' WHERE entry_at>? AND outcome='PENDING' "
                "AND (?=0 OR sent_at IS NULL)",
                (now, int(preserve_sent)),
            )
            db.commit()

    def stats(self, seconds=None, since=0):
        from services.otc_signal_report import journal_stats

        return journal_stats(self, seconds, since)


def select_forecast(
    runtime,
    histories,
    assets,
    quotes,
    now,
    session_end=None,
    reasons=None,
    audit=None,
    excluded_symbols=None,
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
            if not aligned_entry(entry, seconds):
                reasons["waiting_candle_boundary"] += 1
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
            observation = dict(
                version="xgb-audit-v1-" + result["model_sha256"],
                symbol=symbol,
                entry_at=entry,
                expires_at=entry + seconds,
                observed_at=now,
                feature_asof=now,
                direction=direction,
                payout=asset.payout,
                selected=False,
                reason="model_ev_below_margin"
                if ev < MIN_MODEL_EV
                else "no_directional_confirmation",
                gates={"valid_forecast": True},
                probabilities=list(probabilities),
                estimated_ev=ev,
                model_sha=result["model_sha256"],
                calibration_sha=result.get("calibration_sha256", "unknown"),
                selection_policy="xgboost-payout-ev-margin-v2",
            )
            if audit is not None:
                audit.append(observation)
            if symbol in (excluded_symbols or {}):
                continue
            if ev < MIN_MODEL_EV:
                reasons["model_ev_below_margin"] += 1
                continue
            confirmations = build(bars, direction, seconds, now)
            if not confirmations["available"] or confirmations["count"] < 1:
                reasons["no_directional_confirmation"] += 1
                continue
            observation["reason"] = "eligible_not_selected"
            candidate = dict(
                symbol=symbol,
                name=asset.name or symbol,
                entry_at=entry,
                expires_at=entry + seconds,
                direction=direction,
                payout=asset.payout,
                model_sha256=result["model_sha256"],
                model_role="unqualified_experimental",
                selection_policy="xgboost-payout-ev-margin-v2",
                min_model_ev=MIN_MODEL_EV,
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
    if best is not None and audit is not None:
        for row in audit:
            if (row["symbol"], row["expires_at"]) == (
                best["symbol"],
                best["expires_at"],
            ):
                row.update(selected=True, reason="selected_by_model")
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
    from services.otc_regime import describe, describe_context

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
    details = describe_context(report.get("market_context"))
    if details:
        market += "\nКонтекст: " + details
        alignment = report.get("checks", {}).get("alignment")
        alignment_label = {
            "agree": "за направление сигнала",
            "against": "против направления сигнала",
            "neutral": "нейтрально",
        }.get(alignment, "недостаточно данных")
        market += f"\nСогласование с M{report.get('alignment_period', 60) // 60}: {alignment_label}"
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
        self.audit_journal = None
        if runtime is not None:
            from services.otc_candidate_research import CandidateJournal, TickSampler

            self.audit_journal = CandidateJournal(
                journal.path.with_name("xgboost-candidates.db")
            )
            self.audit_sampler = TickSampler()
            self.next_audit_flush = 0

    def observe_audit_tick(self, tick, now):
        if self.audit_journal is not None:
            self.audit_sampler.observe(tick, now)

    def observe(self, history, assets, quotes, session_end):
        self.snapshot = (history, assets, quotes, session_end)

    async def stop(self):
        self.enabled = False
        if self.planning and not self.planning.done():
            self.planning.cancel()
            await asyncio.gather(self.planning, return_exceptions=True)
        await asyncio.to_thread(
            self.journal.cancel_future, self.clock(),
            preserve_sent=getattr(self, "preserve_delivered_on_stop", False),
        )

    async def settle_pending(self, history):
        if self.audit_journal is not None and self.clock() >= self.next_audit_flush:
            self.next_audit_flush = self.clock() + 5
            samples = self.audit_sampler.drain(self.clock())
            if samples:
                try:
                    await asyncio.to_thread(self.audit_journal.samples, samples)
                except Exception:
                    self.audit_sampler.restore(samples)
                    logger.exception("XGBoost audit samples unavailable")
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
        operational_block = await asyncio.to_thread(self.journal.blocked, self.clock())
        history, assets, quotes, end = snapshot
        excluded = await asyncio.to_thread(self.journal.loss_cooldowns, self.clock())
        histories = {symbol: list(bars) for symbol, bars in history.items()}
        reasons = Counter()
        audit = []
        if excluded:
            reasons["symbol_loss_cooldown"] = len(excluded)
        selected = await asyncio.to_thread(
            select_forecast,
            self.runtime,
            histories,
            dict(assets),
            dict(quotes),
            self.clock(),
            end,
            reasons,
            audit,
            excluded,
        )
        for row in audit:
            row["operational_block"] = (
                "symbol_loss_cooldown"
                if row["symbol"] in excluded
                else operational_block
            )
        if self.audit_journal is not None:
            try:
                await asyncio.to_thread(self.audit_journal.record, audit)
                self.audit_sampler.register(audit, self.clock())
            except Exception:
                logger.exception("XGBoost audit journal unavailable")
        if operational_block:
            return
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
        if selected["estimated_ev"] is None or selected["estimated_ev"] < MIN_MODEL_EV:
            logger.info("XGBoost skipped: invalid probabilities at delivery")
            return
        await self.publish(selected, bot, chat_id)

    async def publish(self, selected, bot, chat_id):
        from services.otc_confirmations import build

        selected = dict(selected)
        selected["execution_policy"] = "m2-m5-m15-data-guard-v3"
        selected["settlement_policy"] = "otcharts-canonical-minute-open-v1"
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
        from services.otc_delivery_guard import check_data

        rejection = check_data(selected, self.snapshot, self.clock())
        if rejection:
            logger.info("%s skipped before delivery: %s", self.label, rejection)
            return
        selected["delivery_data_check"] = "fresh-quote8s-catalogue60s-contiguous60m-v1"
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
        rejection = check_data(selected, self.snapshot, self.clock())
        if rejection:
            await asyncio.to_thread(self.journal.finish, identifier, "CANCELLED")
            logger.info("%s cancelled before delivery: %s", self.label, rejection)
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
        async def maintain():
            next_card_update = 0
            while True:
                try:
                    if self.snapshot:
                        await self.settle_pending(self.snapshot[0])
                    if self.clock() >= next_card_update:
                        next_card_update = self.clock() + 10
                        await self.update_cards(bot, chat_id)
                except Exception:
                    logger.exception("Preview result maintenance failed")
                await asyncio.sleep(0.5)

        maintenance = asyncio.create_task(maintain())
        try:
            while True:
                try:
                    if self.planning and self.planning.done():
                        if not self.planning.cancelled() and self.planning.exception():
                            logger.warning("Preview planning failed: %s", type(self.planning.exception()).__name__)
                        self.planning = None
                    now = self.clock()
                    entry = int(now) // 60 * 60 + 60
                    if (self.snapshot and self.enabled
                            and self.planning_window[0] <= entry - now <= self.planning_window[1]
                            and self.attempted != entry
                            and (self.planning is None or self.planning.done())):
                        self.attempted = entry
                        self.planning = asyncio.create_task(self.plan(bot, chat_id, self.snapshot))
                except Exception:
                    logger.exception("Preview planning loop failed")
                await asyncio.sleep(0.5)
        finally:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)
            await self.stop()
