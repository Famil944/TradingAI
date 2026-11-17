"""Read-only three-attempt Bollinger plans. No orders or stake escalation."""

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
from services.otc_delivery_guard import check_data, claim
from services.otc_groq import fresh
from services.otc_ml.preview import MLPreview, PreviewJournal, aligned_entry
from services.otc_shadow_variants import pattern_directions
from services.otc_validation_report import load, resolve

logger = logging.getLogger(__name__)
VERSION = "bollinger-live-three-attempts-v1"


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
    virtual = plan.get("virtual")
    for i, outcome in enumerate(outcomes):
        at = datetime.fromtimestamp(
            plan["entry_at"] + i * seconds, ZoneInfo("Europe/Moscow")
        ).strftime("%H:%M")
        lines.append(
            f"{i + 1}. {at} · {'— отменена' if won else icons.get(outcome, '❔')}"
            + (
                f" · {virtual['stakes'][i] / 100:g} ₽"
                if virtual and virtual.get("status") != "SKIPPED"
                else ""
            )
        )
        won = won or outcome in ("WIN", "DRAW", "UNKNOWN")
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

    def complete(self, identifier, outcomes, result, now):
        with closing(sqlite3.connect(self.path, timeout=1)) as db:
            row = db.execute(
                "SELECT evidence FROM predictions WHERE id=? AND outcome='PENDING'",
                (identifier,),
            ).fetchone()
            if row:
                payload = json.loads(row[0])
                payload["attempt_outcomes"] = outcomes
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
                "SELECT 1 FROM predictions WHERE symbol=? AND entry_at<? AND expires_at>? AND outcome NOT IN ('CANCELLED','NOT_DELIVERED','LATE') LIMIT 1",
                (selected["symbol"], selected["expires_at"], selected["entry_at"]),
            ).fetchone():
                return None
            if db.execute(
                "SELECT 1 FROM predictions WHERE symbol=? AND outcome='PENDING' LIMIT 1",
                (selected["symbol"],),
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
    planning_window = (15, 20)

    def __init__(self, journal, clock=time.time):
        super().__init__(journal, None, clock)
        self.allowed_timeframes = (120, 300, 900)
        self.research = CandidateJournal(
            journal.path.with_name("bollinger-live-ticks.db")
        )
        self.sampler = TickSampler()
        self.next_flush = 0
        self.card_cache = {}
        from services.otc_virtual_wallet import VirtualWallet

        self.wallet = VirtualWallet(journal.path.with_name("virtual-wallet.db"))
        from services.otc_community_cycles import ShadowCycles

        self.shadow = ShadowCycles(journal.path.with_name("community-cycles.db"), clock)
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
        self.shadow.observe(tick, now)

    async def flush(self):
        await self.shadow.flush()
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
            await self.shadow.plan(snapshot)
        except Exception:
            logger.exception("Community cycles audit unavailable")
        history, assets, quotes, end = snapshot
        now = self.clock()
        entry = int(now) // 60 * 60 + 60
        if not 15 <= entry - now <= 20:
            return
        plans = []
        for seconds in self.allowed_timeframes:
            if not aligned_entry(entry, seconds) or (
                end and entry + 3 * seconds + 2 > end
            ):
                continue
            for symbol, minutes in history.items():
                asset = assets.get(symbol)
                if not fresh(asset, quotes.get(symbol), now):
                    continue
                pattern = pattern_directions(list(minutes), seconds, entry - 60).get(
                    "bollinger_reversal"
                )
                if pattern:
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
                            gates={"cycle": True},
                            experiment={"cycle_seconds": seconds},
                        )
                    )
        # Stable ranking, not an invented probability; best payout then longer period.
        used = set()
        for plan in sorted(
            plans, key=lambda r: (-r["payout"], -r["cycle_seconds"], r["symbol"])
        ):
            if (
                plan["symbol"] in used
                or not self.enabled
                or not 15 <= plan["entry_at"] - self.clock() <= 20
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
            )
            if (
                not claimed
                or check_data(plan, self.snapshot, self.clock())
                or not 15 <= plan["entry_at"] - self.clock() <= 20
            ):
                await asyncio.to_thread(self.journal.finish, identifier, "CANCELLED")
                continue
            used.add(plan["symbol"])
            plan["virtual"] = await asyncio.to_thread(self.wallet.enroll, identifier)
            await asyncio.to_thread(
                self.journal.attach_virtual, identifier, plan["virtual"]
            )
            await asyncio.to_thread(self.research.record, [plan])
            self.sampler.register([plan], self.clock())
            try:
                timeout = min(3, max(0.1, plan["entry_at"] - 15 - self.clock()))
                message = await asyncio.wait_for(
                    bot.send_message(chat_id, card(plan), parse_mode=None), timeout
                )
                late = not 15 <= plan["entry_at"] - self.clock() <= 20
                await asyncio.to_thread(
                    self.journal.sent,
                    identifier,
                    self.clock(),
                    message.message_id,
                    late,
                )
                if late:
                    await asyncio.to_thread(self.wallet.cancel, identifier)
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
        _, prices = await asyncio.to_thread(load, self.research.path, 0, self.clock())
        for row in await asyncio.to_thread(self.journal.pending):
            plan = json.loads(row["evidence"])
            if row.get("sent_at") is None and self.clock() >= row["entry_at"]:
                await asyncio.to_thread(self.wallet.cancel, row["id"])
                await asyncio.to_thread(self.journal.finish, row["id"], "NOT_DELIVERED")
                continue
            outcomes = []
            for i in range(3):
                start = plan["entry_at"] + i * plan["cycle_seconds"]
                outcomes.append(
                    resolve(
                        dict(
                            plan,
                            entry_at=start,
                            expires_at=start + plan["cycle_seconds"],
                        ),
                        prices,
                        self.clock(),
                    )
                )
            self.card_cache[row["id"]] = outcomes
            if row.get("sent_at") is not None:
                await asyncio.to_thread(self.wallet.settle, row["id"], outcomes)
            result = live_outcome(outcomes)
            if result != "PENDING":
                await asyncio.to_thread(
                    self.journal.complete, row["id"], outcomes, result, self.clock()
                )

    async def update_cards(self, bot, chat_id):
        with closing(sqlite3.connect(self.journal.path)) as db:
            rows = db.execute(
                "SELECT id,message_id,evidence,outcome FROM predictions WHERE message_id IS NOT NULL"
            ).fetchall()
        for identifier, message_id, evidence, result in rows:
            plan = json.loads(evidence)
            outcomes = self.card_cache.get(identifier)
            if outcomes is None:
                _, prices = await asyncio.to_thread(
                    load, self.research.path, 0, self.clock()
                )
                outcomes = [
                    resolve(
                        dict(
                            plan,
                            entry_at=plan["entry_at"] + i * plan["cycle_seconds"],
                            expires_at=plan["entry_at"]
                            + (i + 1) * plan["cycle_seconds"],
                        ),
                        prices,
                        self.clock(),
                    )
                    for i in range(3)
                ]
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
