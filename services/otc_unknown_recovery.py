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
        fetch_limit=1,
        foreground=None,
        quota_reserve=100,
        allow_backfill=True,
    ):
        self.root = Path(root)
        self.provider = provider
        self.groq = groq
        self.clock = clock
        self.interval = max(15, interval)
        self.fetch_limit = max(1, fetch_limit)
        # Local reconciliation remains active during live signals.
        # Only network backfills pause until the owner stops the session.
        self.foreground = foreground
        self.allow_backfill = allow_backfill
        self.quota_reserve = max(100, int(quota_reserve))
        self.archive_path = self.root / "data" / "market-history.db"
        self.live_path = self.root / "data" / "community-live.db"
        self.bollinger_path = self.root / "data" / "bollinger-cycles.db"
        self.next_retry = {}
        self.last_scan = 0
        self.last_recovered = 0
        self.last_pending = 0
        self.paused_for_signals = False

    def _foreground_running(self):
        return self.foreground is not None and self.foreground.running()

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

    def _reconstruct_cycle(
        self, symbol, entry_at, seconds, direction, attempts, sources
    ):
        """Rebuild the pre-published 3-attempt plan from canonical history.

        A later attempt is eligible only when every earlier attempt is LOSS.
        Existing known outcomes are preserved. Missing/UNKNOWN/PENDING attempts
        are filled only from exact unrevised OTCharts historical opens.
        """
        attempts = list(attempts or [])
        sources = list(sources or [])
        audits = []
        for index in range(3):
            if index < len(attempts) and attempts[index] in ("WIN", "DRAW"):
                return (
                    attempts[: index + 1],
                    sources[: index + 1],
                    attempts[index],
                    audits,
                    False,
                )
            if index < len(attempts) and attempts[index] == "LOSS":
                continue

            start = int(entry_at + index * seconds)
            end = start + seconds
            entry_price = self._price(symbol, start)
            expiry_price = self._price(symbol, end)
            state = _canonical_outcome(
                self.archive_path, symbol, start, end, direction
            )
            if state is None or entry_price is None or expiry_price is None:
                if len(attempts) <= index:
                    attempts.extend(["PENDING"] * (index - len(attempts)))
                    attempts.append("UNKNOWN")
                else:
                    attempts[index] = "UNKNOWN"
                if len(sources) <= index:
                    sources.extend(["unconfirmed"] * (index + 1 - len(sources)))
                else:
                    sources[index] = "unconfirmed"
                return attempts, sources, "UNKNOWN", audits, True

            if len(attempts) <= index:
                attempts.extend(["PENDING"] * (index - len(attempts)))
                attempts.append(state)
            else:
                attempts[index] = state
            if len(sources) <= index:
                sources.extend(["unconfirmed"] * (index - len(sources)))
                sources.append("otcharts_history")
            else:
                sources[index] = "otcharts_history"
            audits.append(
                dict(
                    index=index,
                    direction=direction,
                    entry_price=entry_price,
                    expiry_price=expiry_price,
                    outcome=state,
                )
            )
            if state in ("WIN", "DRAW"):
                return (
                    attempts[: index + 1],
                    sources[: index + 1],
                    state,
                    audits,
                    False,
                )

        return attempts[:3], sources[:3], "LOSS", audits, False

    def _recover_live(self):
        if not self.live_path.is_file():
            return [], set()
        journal = LiveStrategyJournal(self.live_path)
        audits, unresolved = [], set()
        for row in self._live_unknowns():
            try:
                old_attempts = json.loads(row.get("attempts_json") or "[]")
                old_sources = json.loads(row.get("attempt_sources_json") or "[]")
                seconds = int(row["seconds"])
                attempts, sources, state, recovered, missing = self._reconstruct_cycle(
                    row["symbol"],
                    row["entry_at"],
                    seconds,
                    row["direction"],
                    old_attempts,
                    old_sources,
                )
                if (
                    attempts != old_attempts
                    or sources != old_sources
                    or state != row["outcome"]
                ):
                    journal.save_outcome(
                        row["id"], attempts, sources, state, self.clock()
                    )
                for item in recovered:
                    audits.append(
                        dict(
                            id="live:" + row["id"] + ":" + str(item["index"]),
                            direction=item["direction"],
                            entry_price=item["entry_price"],
                            expiry_price=item["expiry_price"],
                            outcome=item["outcome"],
                        )
                    )
                if missing:
                    unresolved.add(row["symbol"])
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
                    old_attempts = list(plan.get("attempt_outcomes") or [])
                    seconds = int(plan.get("cycle_seconds") or 0)
                    if seconds not in (120, 300, 900):
                        unresolved.add(row["symbol"])
                        continue
                    attempts, sources, outcome, recovered, missing = (
                        self._reconstruct_cycle(
                            row["symbol"],
                            row["entry_at"],
                            seconds,
                            row["direction"],
                            old_attempts,
                            plan.get("attempt_sources")
                            or ["unconfirmed"] * len(old_attempts),
                        )
                    )
                    old_sources = plan.get("attempt_sources") or [
                        "unconfirmed"
                    ] * len(old_attempts)
                    if (
                        attempts != old_attempts
                        or sources != old_sources
                        or outcome != row["outcome"]
                    ):
                        plan["attempt_outcomes"] = attempts
                        plan["recovered_unknown_at"] = self.clock()
                        plan["attempt_sources"] = sources
                        if outcome in ("WIN", "DRAW"):
                            plan["stopped_after_index"] = attempts.index(outcome)
                        elif outcome == "UNKNOWN":
                            plan["stopped_after_index"] = attempts.index("UNKNOWN")
                        else:
                            plan.pop("stopped_after_index", None)
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
                    for item in recovered:
                        audits.append(
                            dict(
                                id="bollinger:" + row["id"] + ":" + str(item["index"]),
                                direction=item["direction"],
                                entry_price=item["entry_price"],
                                expiry_price=item["expiry_price"],
                                outcome=item["outcome"],
                            )
                        )
                    if missing:
                        unresolved.add(row["symbol"])
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    unresolved.add(row.get("symbol", ""))
            db.commit()
        return audits, {s for s in unresolved if s}

    def recover_local(self):
        live_audits, live_missing = self._recover_live()
        boll_audits, boll_missing = self._recover_bollinger()
        return live_audits + boll_audits, live_missing | boll_missing

    async def _backfill(self, symbols):
        if not self.allow_backfill or self._foreground_running():
            return
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
            if not isinstance(remaining, int) or remaining <= self.quota_reserve:
                LOG.warning("UNKNOWN recovery paused: OTCharts request reserve is low")
                return
        except Exception:
            # Do not spend signal quota when its remaining balance is unknown.
            return
        for symbol in eligible:
            if self._foreground_running():
                return
            try:
                bars = await self.provider.candles(symbol, self.clock())
                self.next_retry[symbol] = self.clock() + (900 if bars else 600)
                LOG.info(
                    "UNKNOWN recovery backfilled %s: %d canonical bars",
                    symbol,
                    len(bars),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.next_retry[symbol] = self.clock() + 120
                LOG.info(
                    "UNKNOWN recovery will retry %s after network/data error: %s",
                    symbol,
                    type(exc).__name__,
                )

    async def sweep(self):
        self.paused_for_signals = self._foreground_running()
        self.last_scan = self.clock()
        audits, missing = await asyncio.to_thread(self.recover_local)
        if missing and self.allow_backfill and not self.paused_for_signals:
            await self._backfill(missing)
            more_audits, missing = await asyncio.to_thread(self.recover_local)
            known = {item["id"] for item in audits}
            audits.extend(item for item in more_audits if item["id"] not in known)
        self.last_recovered = len(audits)
        self.last_pending = len(missing)
        if audits and self.groq is not None and self.allow_backfill and not self.paused_for_signals:
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
