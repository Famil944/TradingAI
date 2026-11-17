"""Background recovery of historically UNKNOWN OTC outcomes.

Only canonical OTCharts history may change an outcome. Groq is audit-only: it
may verify deterministic arithmetic after prices are recovered, but its answer
never supplies prices and never creates a trading signal.
"""

import asyncio
import json
import logging
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from services.otc_strategies_live import (
    LiveStrategyJournal,
    _canonical_outcome,
    reconcile_unknown,
)

LOG = logging.getLogger(__name__)


class UnknownRecovery:
    def __init__(
        self,
        root,
        provider,
        groq=None,
        *,
        clock=time.time,
        interval=60,
        fetch_limit=3,
    ):
        self.root = Path(root)
        self.provider = provider
        self.groq = groq
        self.clock = clock
        self.interval = max(15, interval)
        self.fetch_limit = max(1, fetch_limit)
        self.archive_path = self.root / "data" / "market-history.db"
        self.live_path = self.root / "data" / "community-live.db"
        self.bollinger_path = self.root / "data" / "bollinger-cycles.db"
        self.next_retry = {}
        self.last_scan = 0
        self.last_recovered = 0
        self.last_pending = 0

    @staticmethod
    def _open(path):
        db = sqlite3.connect(path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def _live_unknowns(self):
        if not self.live_path.is_file():
            return []
        with closing(self._open(self.live_path)) as db:
            try:
                return [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM signals WHERE sent_at IS NOT NULL "
                        "AND outcome='UNKNOWN' ORDER BY COALESCE(resolved_at,expires_at),id"
                    )
                ]
            except sqlite3.OperationalError:
                return []

    def _bollinger_unknowns(self):
        if not self.bollinger_path.is_file():
            return []
        with closing(self._open(self.bollinger_path)) as db:
            try:
                return [
                    dict(row)
                    for row in db.execute(
                        "SELECT id,symbol,entry_at,expires_at,direction,evidence,"
                        "sent_at,outcome,resolved_at FROM predictions "
                        "WHERE sent_at IS NOT NULL AND outcome='UNKNOWN' "
                        "ORDER BY COALESCE(resolved_at,expires_at),id"
                    )
                ]
            except sqlite3.OperationalError:
                return []

    def _price(self, symbol, at):
        if not self.archive_path.is_file():
            return None
        with closing(self._open(self.archive_path)) as db:
            try:
                row = db.execute(
                    "SELECT open FROM bars WHERE symbol=? AND time=? "
                    "AND source='otcharts_history' AND revisions=0",
                    (symbol, int(at)),
                ).fetchone()
            except sqlite3.OperationalError:
                return None
        if not row:
            return None
        value = row[0]
        return (
            float(value)
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 0
            else None
        )

    def _recover_live(self):
        if not self.live_path.is_file():
            return [], set()
        journal = LiveStrategyJournal(self.live_path)
        audits, unresolved = [], set()
        for row in self._live_unknowns():
            try:
                old_attempts = json.loads(row.get("attempts_json") or "[]")
                if "UNKNOWN" not in old_attempts:
                    continue
                index = old_attempts.index("UNKNOWN")
                seconds = int(row["seconds"])
                start = int(row["entry_at"] + index * seconds)
                end = start + seconds
                entry_price = self._price(row["symbol"], start)
                expiry_price = self._price(row["symbol"], end)
                attempts, sources, state = reconcile_unknown(
                    row, {}, self.clock(), self.archive_path
                )
                if attempts == old_attempts:
                    unresolved.add(row["symbol"])
                    continue
                journal.save_outcome(
                    row["id"], attempts, sources, state, self.clock()
                )
                recovered = attempts[index]
                if (
                    recovered in ("WIN", "LOSS", "DRAW")
                    and entry_price is not None
                    and expiry_price is not None
                ):
                    audits.append(
                        dict(
                            id="live:" + row["id"] + ":" + str(index),
                            direction=row["direction"],
                            entry_price=entry_price,
                            expiry_price=expiry_price,
                            outcome=recovered,
                        )
                    )
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                unresolved.add(row.get("symbol", ""))
        return audits, {s for s in unresolved if s}

    def _recover_bollinger(self):
        if not self.bollinger_path.is_file():
            return [], set()
        audits, unresolved = [], set()
        with closing(self._open(self.bollinger_path)) as db:
            for row in self._bollinger_unknowns():
                try:
                    plan = json.loads(row["evidence"])
                    attempts = list(plan.get("attempt_outcomes") or [])
                    if "UNKNOWN" not in attempts:
                        continue
                    index = attempts.index("UNKNOWN")
                    seconds = int(plan.get("cycle_seconds") or 0)
                    if seconds not in (120, 300, 900):
                        unresolved.add(row["symbol"])
                        continue
                    start = int(row["entry_at"] + index * seconds)
                    end = start + seconds
                    entry_price = self._price(row["symbol"], start)
                    expiry_price = self._price(row["symbol"], end)
                    state = _canonical_outcome(
                        self.archive_path,
                        row["symbol"],
                        start,
                        end,
                        row["direction"],
                    )
                    if state is None:
                        unresolved.add(row["symbol"])
                        continue
                    attempts[index] = state
                    plan["attempt_outcomes"] = attempts
                    plan["recovered_unknown_at"] = self.clock()
                    plan["stopped_after_index"] = index
                    if state in ("WIN", "DRAW"):
                        outcome = state
                    elif index == 2 and all(x == "LOSS" for x in attempts[:3]):
                        outcome = "LOSS"
                    else:
                        # Later attempts were not confirmed as entered after UNKNOWN.
                        outcome = "UNKNOWN"
                    db.execute(
                        "UPDATE predictions SET evidence=?,outcome=?,resolved_at=? "
                        "WHERE id=? AND outcome='UNKNOWN'",
                        (
                            json.dumps(plan, ensure_ascii=False, allow_nan=False),
                            outcome,
                            self.clock(),
                            row["id"],
                        ),
                    )
                    if entry_price is not None and expiry_price is not None:
                        audits.append(
                            dict(
                                id="bollinger:" + row["id"] + ":" + str(index),
                                direction=row["direction"],
                                entry_price=entry_price,
                                expiry_price=expiry_price,
                                outcome=state,
                            )
                        )
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    unresolved.add(row.get("symbol", ""))
            db.commit()
        return audits, {s for s in unresolved if s}

    def recover_local(self):
        live_audits, live_missing = self._recover_live()
        boll_audits, boll_missing = self._recover_bollinger()
        return live_audits + boll_audits, live_missing | boll_missing

    async def _backfill(self, symbols):
        now = self.clock()
        eligible = [
            symbol
            for symbol in sorted(symbols)
            if now >= self.next_retry.get(symbol, 0)
        ][: self.fetch_limit]
        if not eligible:
            return
        try:
            plan = await self.provider.usage()
            remaining = getattr(plan, "remaining", None)
            if isinstance(remaining, int) and remaining <= 10:
                LOG.warning("UNKNOWN recovery paused: OTCharts request reserve is low")
                return
        except Exception:
            # Usage is advisory; a later candle call will still fail closed.
            pass
        for symbol in eligible:
            try:
                bars = await self.provider.candles(symbol, self.clock())
                self.next_retry[symbol] = self.clock() + (600 if bars else 300)
                LOG.info(
                    "UNKNOWN recovery backfilled %s: %d canonical bars",
                    symbol,
                    len(bars),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.next_retry[symbol] = self.clock() + 60
                LOG.info(
                    "UNKNOWN recovery will retry %s after network/data error: %s",
                    symbol,
                    type(exc).__name__,
                )

    async def sweep(self):
        self.last_scan = self.clock()
        audits, missing = await asyncio.to_thread(self.recover_local)
        if missing:
            await self._backfill(missing)
            more_audits, missing = await asyncio.to_thread(self.recover_local)
            known = {item["id"] for item in audits}
            audits.extend(item for item in more_audits if item["id"] not in known)
        self.last_recovered = len(audits)
        self.last_pending = len(missing)
        if audits and self.groq is not None:
            # Groq only audits arithmetic already established by canonical prices.
            await self.groq.audit_recovery(audits)
        if audits:
            LOG.info(
                "UNKNOWN recovery updated %d historical attempt(s); unresolved symbols=%d",
                len(audits),
                len(missing),
            )
        return len(audits), len(missing)

    async def run(self):
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("UNKNOWN recovery sweep failed: %s", type(exc).__name__)
            await asyncio.sleep(self.interval)
