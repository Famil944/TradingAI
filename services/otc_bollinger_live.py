"""Read-only three-attempt Bollinger plans. No orders or stake escalation."""

from services.otc_market import symbol_priority, ACTIVE_TIMEFRAMES, EXCLUDED_SYMBOLS

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from services.otc_candidate_research import CandidateJournal, TickSampler
from services.otc_delivery_guard import check_data, claim, completed
from services.otc_delivery_timing import MIN_LEAD, MAX_LEAD, in_window, send_timeout
from services.otc_groq import fresh
from services.otc_ml.preview import MLPreview, PreviewJournal, aligned_entry
from services.otc_shadow_variants import pattern_directions
from services.otc_strategies_live import evaluate_cycle
from services.otc_validation_report import load

logger = logging.getLogger(__name__)
VERSION = "bollinger-reentry-rejection-v2"


def quality_rejection(pattern):
    """Require a closed-bar re-entry and at least 60% rejection of the extreme."""
    import math
    candle = pattern.get("candle") or {}
    values = [candle.get(k) for k in ("open", "high", "low", "close")]
    values += [pattern.get("lower"), pattern.get("upper")]
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        return "missing_evidence"
    opening, high, low, close, lower, upper = values
    if not low <= min(opening, close) <= max(opening, close) <= high or high <= low:
        return "invalid_candle"
    if not lower < close < upper:
        return "no_reentry"
    direction = pattern.get("direction")
    if direction not in ("UP", "DOWN"):
        return "invalid_direction"
    rebound = (close - low) if direction == "UP" else (high - close)
    if rebound / (high - low) < 0.6:
        return "weak_rejection"
    return None


def card(plan, outcomes=None, result=None):
    outcomes = outcomes or ["PENDING"] * 3
    icons = {"WIN": "✅", "LOSS": "❌", "DRAW": "➖", "UNKNOWN": "❔", "PENDING": "⏳"}
    seconds = plan["cycle_seconds"]
    arrow = "🟢⬆️" if plan["direction"] == "UP" else "🔴⬇️"
    lines = [
        f"БОЛЛИНДЖЕР ×3 · M{seconds // 60} {arrow}",
        plan.get("name", plan["symbol"]),
    ]
    won = False
    stopped_after = plan.get("stopped_after_index")
    virtual = plan.get("virtual")
    for i in range(3):
        outcome = outcomes[i] if i < len(outcomes) else "PENDING"
        at = datetime.fromtimestamp(
            plan["entry_at"] + i * seconds, ZoneInfo("Europe/Moscow")
        ).strftime("%H:%M")
        cancelled = won or (
            isinstance(stopped_after, int) and i > stopped_after
        )
        lines.append(
            f"{i + 1}. {at} · {'— отменена' if cancelled else icons.get(outcome, '❔')}"
            + (
                f" · {virtual['stakes'][i] / 100:g} ₽"
                if virtual and virtual.get("status") != "SKIPPED"
                else ""
            )
        )
        won = (
            won
            or outcome in ("WIN", "DRAW", "UNKNOWN")
            or (isinstance(stopped_after, int) and i >= stopped_after)
        )
    lines.append("МСК · после WIN остановиться")
    lines.append("Повтор — только после LOSS; при ничьей или ❔ пропустить остаток.")
    if result:
        lines.append(
            "Цикл: "
            + {
                "WIN": "✅ WIN",
                "LOSS": "❌ LOSS",
                "DRAW": "➖ Ничья",
                "UNKNOWN": "❔ Нет данных",
                "LATE": "Опоздание — не входить",
                "NOT_DELIVERED": "Не доставлен",
            }.get(result, result)
        )
    if virtual:
        if virtual.get("status") == "SKIPPED":
            lines.append("Вирт. счёт: пропуск — недостаточно средств")
        elif "delta" in virtual:
            lines.append(
                f"Вирт. итог известных исходов: {virtual['delta'] / 100:+.2f} ₽"
            )
    return "\n".join(lines)


def live_outcome(outcomes):
    # Live instructions stop on missing data/draw, unlike hypothetical any-of-three.
    for item in outcomes:
        if item != "LOSS":
            return item
    return "LOSS"


class CycleJournal(PreviewJournal):
    def attach_virtual(self, identifier, virtual):
        with closing(sqlite3.connect(self.path, timeout=1)) as db:
            row = db.execute(
                "SELECT evidence FROM predictions WHERE id=?", (identifier,)
            ).fetchone()
            if row:
                payload = json.loads(row[0])
                payload["virtual"] = virtual
                db.execute(
                    "UPDATE predictions SET evidence=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), identifier),
                )
                db.commit()

    def complete(self, identifier, outcomes, result, now, sources=None):
        with closing(sqlite3.connect(self.path, timeout=1)) as db:
            row = db.execute(
                "SELECT evidence FROM predictions WHERE id=? AND outcome='PENDING'",
                (identifier,),
            ).fetchone()
            if row:
                payload = json.loads(row[0])
                payload["attempt_outcomes"] = outcomes
                if sources is not None:
                    payload["attempt_sources"] = sources
                db.execute(
                    "UPDATE predictions SET evidence=?,outcome=?,resolved_at=? WHERE id=? AND outcome='PENDING'",
                    (json.dumps(payload, ensure_ascii=False), result, now, identifier),
                )
                db.commit()

    def reserve(self, selected, now):
        seconds = selected["cycle_seconds"]
        if not aligned_entry(selected["entry_at"], seconds):
            return None
        with closing(sqlite3.connect(self.path, timeout=1)) as db:
            db.execute("BEGIN IMMEDIATE")
            if self._blocked(db, now, 3 * seconds):
                return None
            if db.execute(
                "SELECT 1 FROM predictions WHERE symbol=? "
                "AND expires_at-entry_at=? AND entry_at<? AND expires_at>? "
                "AND outcome NOT IN ('CANCELLED','NOT_DELIVERED','LATE') LIMIT 1",
                (selected["symbol"], 3 * seconds,
                 selected["expires_at"], selected["entry_at"]),
            ).fetchone():
                return None
            if db.execute(
                "SELECT 1 FROM predictions WHERE symbol=? "
                "AND expires_at-entry_at=? AND outcome='PENDING' LIMIT 1",
                (selected["symbol"], 3 * seconds),
            ).fetchone():
                return None
            if db.execute(
                "SELECT 1 FROM predictions WHERE symbol=? AND outcome='LOSS' AND resolved_at>? LIMIT 1",
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
                        VERSION,
                        json.dumps(selected, allow_nan=False),
                    ),
                )
                db.commit()
                return identifier
            except sqlite3.IntegrityError:
                return None

    def stats(self, seconds=None, since=0):
        from collections import Counter

        from services.otc_signal_report import read_journal

        rows = [
            r
            for r in read_journal(self, since, time.time())
            if seconds is None or r["expires_at"] - r["entry_at"] == 3 * seconds
        ]
        counts = Counter(r["outcome"] for r in rows)
        w, loss = counts["WIN"], counts["LOSS"]
        rate = f"{100 * w / (w + loss):.1f}%" if w + loss else "—"
        stages = Counter()
        for row in rows:
            outcomes = json.loads(row["evidence"]).get("attempt_outcomes", [])
            if row["outcome"] == "WIN" and "WIN" in outcomes:
                stages[outcomes.index("WIN") + 1] += 1
        return f"Боллинджер ×3 · {'все' if seconds is None else 'M' + str(seconds // 60)}\nWIN {w} · LOSS {loss} · WR {rate}\nПобеды: ① {stages[1]} · ② {stages[2]} · ③ {stages[3]}\nНичьи {counts['DRAW']} · ждут {counts['PENDING']} · нет данных {counts['UNKNOWN']}\nЭто винрейт циклов, без расчёта ставок."


class BollingerCycles(MLPreview):
    label = "Боллинджер ×3"
    planning_window = (MIN_LEAD, MAX_LEAD)

    def __init__(self, journal, clock=time.time, subscriber_relay=None):
        super().__init__(journal, None, clock)
        self.subscriber_relay = subscriber_relay
        self.allowed_timeframes = ACTIVE_TIMEFRAMES
        self.research = CandidateJournal(
            journal.path.with_name("bollinger-live-ticks.db")
        )
        self.archive_path = journal.path.with_name("market-history.db")
        self.sampler = TickSampler()
        self.next_flush = 0
        self.card_cache = {}
        from services.otc_virtual_wallet import VirtualWallet

        self.wallet = VirtualWallet(journal.path.with_name("virtual-wallet.db"))
        # Required candidate and tick pipeline for the LIVE S2–S4 publisher.
        # This is independent of the retired early/H1 experiments.
        from services.otc_community_cycles import ShadowCycles
        self.live_candidates = ShadowCycles(journal.path.with_name("community-cycles.db"), clock)
        for row in journal.pending():
            self.sampler.register([json.loads(row["evidence"])], self.clock())
        with closing(sqlite3.connect(journal.path)) as db:
            cancelled = db.execute(
                "SELECT id FROM predictions WHERE outcome IN ('CANCELLED','LATE','NOT_DELIVERED')"
            ).fetchall()
        for row in cancelled:
            self.wallet.cancel(row[0])

    def observe_tick(self, tick, now):
        self.sampler.observe(tick, now)
        self.live_candidates.observe(tick, now)

    async def flush(self):
        await self.live_candidates.flush()
        samples = self.sampler.drain(self.clock())
        if samples:
            try:
                await asyncio.to_thread(self.research.samples, samples)
            except Exception:
                self.sampler.restore(samples)
                raise

    async def stop(self):
        await super().stop()
        await self.flush()

    async def plan(self, bot, chat_id, snapshot):
        try:
            await self.live_candidates.plan(snapshot)
        except Exception:
            logger.exception("Live S2-S4 candidate generation unavailable")
        history, assets, quotes, end = snapshot
        now = self.clock()
        entry = int(now) // 60 * 60 + 60
        if not in_window(entry, now):
            return
        plans = []
        for seconds in self.allowed_timeframes:
            if seconds not in ACTIVE_TIMEFRAMES:
                continue
            if not aligned_entry(entry, seconds) or (
                end and entry + 3 * seconds + 2 > end
            ):
                continue
            for symbol, minutes in history.items():
                if symbol in EXCLUDED_SYMBOLS:
                    continue
                asset = assets.get(symbol)
                if not fresh(asset, quotes.get(symbol), now):
                    continue
                pattern = pattern_directions(list(minutes), seconds, entry - 60).get(
                    "bollinger_reversal"
                )
                if pattern and quality_rejection(pattern) is None:
                    plans.append(
                        dict(
                            symbol=symbol,
                            name=asset.name or symbol,
                            entry_at=entry,
                            expires_at=entry + 3 * seconds,
                            cycle_seconds=seconds,
                            direction=pattern["direction"],
                            payout=asset.payout,
                            feature_asof=entry - 60,
                            model_sha256=VERSION,
                            evidence={"pattern": pattern},
                            version=VERSION,
                            observed_at=now,
                            gates={"cycle": True, "reentry": True, "rejection_min": 0.6},
                            experiment={"cycle_seconds": seconds},
                        )
                    )
        # Stable ranking, not an invented probability; best payout then longer period.
        used = set()
        for plan in sorted(
            plans, key=lambda r: (symbol_priority(r["symbol"]), -r["payout"], -r["cycle_seconds"], r["symbol"])
        ):
            if (
                (plan["symbol"], plan["cycle_seconds"]) in used
                or not self.enabled
                or not in_window(plan["entry_at"], self.clock())
            ):
                continue
            if check_data(plan, self.snapshot, self.clock()):
                continue
            identifier = await asyncio.to_thread(
                self.journal.reserve, plan, self.clock()
            )
            if not identifier:
                continue
            claimed = await asyncio.to_thread(
                claim,
                self.journal.path.with_name("signal-delivery.db"),
                plan,
                self.label,
                self.clock(),
                exclusive_timeframe=True,
            )
            if (
                not claimed
                or check_data(plan, self.snapshot, self.clock())
                or send_timeout(plan["entry_at"], self.clock()) <= 0
            ):
                await asyncio.to_thread(self.journal.finish, identifier, "CANCELLED")
                continue
            used.add((plan["symbol"], plan["cycle_seconds"]))
            plan["virtual"] = await asyncio.to_thread(self.wallet.enroll, identifier)
            await asyncio.to_thread(
                self.journal.attach_virtual, identifier, plan["virtual"]
            )
            await asyncio.to_thread(self.research.record, [plan])
            self.sampler.register([plan], self.clock())
            try:
                timeout = send_timeout(plan["entry_at"], self.clock())
                message = await asyncio.wait_for(
                    bot.send_message(chat_id, card(plan), parse_mode=None), timeout
                )
                late = not in_window(plan["entry_at"], self.clock())
                sent_at = self.clock()
                await asyncio.to_thread(
                    self.journal.sent,
                    identifier,
                    sent_at,
                    message.message_id,
                    late,
                )
                if late:
                    await asyncio.to_thread(self.wallet.cancel, identifier)
                    try:
                        await asyncio.wait_for(bot.edit_message_text(
                            "⚠️ Сигнал опоздал — НЕ ВХОДИТЬ.\n" + plan["name"],
                            chat_id=chat_id, message_id=message.message_id,
                            parse_mode=None,
                        ), 3)
                    except Exception:
                        logger.warning("Could not mark a late S1 card")
                elif self.subscriber_relay is not None:
                    try:
                        await self.subscriber_relay.publish_now(
                            bot,
                            dict(
                                id="main:" + identifier,
                                origin="main",
                                strategy="bollinger_live",
                                version=plan["model_sha256"],
                                label=self.label,
                                seconds=plan["cycle_seconds"],
                                symbol=plan["name"],
                                direction=plan["direction"],
                                created_at=plan.get("observed_at", sent_at),
                                sent_at=sent_at,
                                entry_at=plan["entry_at"],
                                expires_at=plan["expires_at"],
                                resolved_at=plan["expires_at"],
                                outcome="PENDING",
                                attempts=[],
                            ),
                        )
                    except Exception:
                        logger.exception("Subscriber relay failed after confirmed S1 delivery")
                logger.info(
                    "Bollinger cycle sent: %s M%d lead=%.1f",
                    plan["symbol"],
                    plan["cycle_seconds"] // 60,
                    plan["entry_at"] - self.clock(),
                )
            except Exception as exc:
                await asyncio.to_thread(self.wallet.cancel, identifier)
                await asyncio.to_thread(
                    self.journal.finish, identifier, "NOT_DELIVERED"
                )
                logger.warning("Bollinger cycle send: %s", type(exc).__name__)

    async def settle_pending(self, history):
        if self.clock() < self.next_flush:
            return
        self.next_flush = self.clock() + 2
        await self.flush()
        pending = await asyncio.to_thread(self.journal.pending)
        if not pending:
            return
        since = min(row["entry_at"] for row in pending)
        _, prices = await asyncio.to_thread(load, self.research.path, since, self.clock())
        for row in pending:
            plan = json.loads(row["evidence"])
            if row.get("sent_at") is None and self.clock() >= row["entry_at"]:
                await asyncio.to_thread(self.wallet.cancel, row["id"])
                await asyncio.to_thread(self.journal.finish, row["id"], "NOT_DELIVERED")
                continue
            attempts, sources, result = await asyncio.to_thread(
                evaluate_cycle,
                dict(plan, seconds=plan["cycle_seconds"]),
                prices,
                self.clock(),
                self.archive_path,
            )
            outcomes = (attempts + ["PENDING"] * 3)[:3]
            self.card_cache[row["id"]] = outcomes
            if row.get("sent_at") is not None:
                await asyncio.to_thread(self.wallet.settle, row["id"], outcomes)
            if result != "PENDING":
                await asyncio.to_thread(
                    self.journal.complete,
                    row["id"],
                    outcomes,
                    result,
                    self.clock(),
                    sources,
                )
                if row.get("sent_at") is not None and attempts:
                    await asyncio.to_thread(
                        completed,
                        self.journal.path.with_name("signal-delivery.db"),
                        plan,
                        self.label,
                        plan["entry_at"] + len(attempts) * plan["cycle_seconds"],
                    )

    async def update_cards(self, bot, chat_id):
        with closing(sqlite3.connect(self.journal.path)) as db:
            rows = db.execute(
                "SELECT id,message_id,evidence,outcome FROM predictions WHERE message_id IS NOT NULL"
            ).fetchall()
        for identifier, message_id, evidence, result in rows:
            plan = json.loads(evidence)
            outcomes = self.card_cache.get(identifier)
            if plan.get("recovered_unknown_at") and plan.get("attempt_outcomes"):
                outcomes = list(plan["attempt_outcomes"])
            elif outcomes is None:
                _, prices = await asyncio.to_thread(
                    load, self.research.path, plan["entry_at"], self.clock()
                )
                attempts, _, _ = await asyncio.to_thread(
                    evaluate_cycle,
                    dict(plan, seconds=plan["cycle_seconds"]),
                    prices,
                    self.clock(),
                    self.archive_path,
                )
                outcomes = (attempts + ["PENDING"] * 3)[:3]
            text = card(plan, outcomes, None if result == "PENDING" else result)
            if plan.get("virtual") and result != "PENDING":
                plan["virtual"]["delta"] = await asyncio.to_thread(
                    self.wallet.delta, identifier
                )
                text = card(plan, outcomes, result)
            cache_key = (identifier, "text")
            if self.card_cache.get(cache_key) == text:
                continue
            try:
                await asyncio.wait_for(
                    bot.edit_message_text(
                        text, chat_id=chat_id, message_id=message_id, parse_mode=None
                    ),
                    3,
                )
                self.card_cache[cache_key] = text
                if result != "PENDING":
                    await asyncio.to_thread(self.journal.card_updated, identifier)
            except Exception as exc:
                if "message is not modified" in str(exc).lower():
                    self.card_cache[cache_key] = text
                    if result != "PENDING":
                        await asyncio.to_thread(self.journal.card_updated, identifier)
                else:
                    logger.warning("Bollinger card update: %s", type(exc).__name__)
