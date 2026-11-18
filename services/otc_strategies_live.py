"""Three independent, published OTC forecast strategies (no orders or wallet).

Candidates are produced prospectively by the existing causal research engine.
A candidate only becomes a LIVE forecast after Telegram confirms delivery
*before* entry. Older research candidates remain research, never backfilled.
"""
import asyncio
import hashlib
import json
import logging
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from services.otc_candidate_report import outcome as direction_outcome
from services.otc_community_cycles import METHODS
from services.otc_community_cycles import VERSION as RESEARCH_VERSION
from services.otc_delivery_guard import claim, completed
from services.otc_public_broadcast import public_text
from services.otc_validation_report import load

LOG = logging.getLogger(__name__)
LIVE_VERSION = "independent-delivered-cycles-v1"
MIN_LEAD = 5
MAX_LEAD = 30
RECOVERY_GRACE = 180
POLL_INTERVAL = 1.0
# S1 prepares entries 15-20 seconds ahead. Let it claim first; S2-S4
# publish during the following window if S1 did not select that instrument.
SECONDARY_MAX_LEAD = 14
STRATEGY_PRIORITY = {"bb_reentry": 0, "break_retest": 1, "ema_pullback": 2}


def _connect(path):
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def display_name(symbol, name=None):
    """Prefer OTCharts catalogue name; prettify legacy technical symbols."""
    if name and str(name).strip() and str(name).strip() != str(symbol).strip():
        return str(name).strip()
    raw = str(symbol or "").strip().lstrip("#")
    if raw.lower().endswith("_otc"):
        raw = raw[:-4]
    if len(raw) == 6 and raw.isalpha():
        raw = raw[:3] + "/" + raw[3:]
    return (raw or str(symbol or "OTC")) + " OTC"


class LiveStrategyJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(_connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS signals(
                    id TEXT PRIMARY KEY,
                    strategy TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    seconds INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    entry_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    direction TEXT NOT NULL,
                    version TEXT NOT NULL,
                    sent_at REAL,
                    message_id INTEGER,
                    outcome TEXT NOT NULL DEFAULT 'RESERVED',
                    attempts_json TEXT NOT NULL DEFAULT '[]',
                    attempt_sources_json TEXT NOT NULL DEFAULT '[]',
                    resolved_at REAL,
                    card_sha TEXT,
                    UNIQUE(strategy,seconds,entry_at)
                );
                CREATE INDEX IF NOT EXISTS live_sent ON signals(sent_at);
                CREATE INDEX IF NOT EXISTS live_active ON signals(outcome,expires_at);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(signals)")}
            if "display_name" not in columns:
                db.execute("ALTER TABLE signals ADD COLUMN display_name TEXT")
            db.commit()

    def reserve(self, candidate, now):
        method = candidate["experiment"]["method"]
        seconds = candidate["experiment"]["cycle_seconds"]
        ident = "strategy:" + hashlib.sha256(
            json.dumps(
                [LIVE_VERSION, method, seconds, candidate["symbol"],
                 candidate["entry_at"], candidate["direction"]],
                separators=(",", ":"), ensure_ascii=False,
            ).encode()
        ).hexdigest()[:24]
        with closing(_connect(self.path)) as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "INSERT OR IGNORE INTO signals"
                "(id,strategy,symbol,seconds,created_at,entry_at,expires_at,direction,version,display_name)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ident, method, candidate["symbol"], seconds, now,
                 candidate["entry_at"], candidate["expires_at"],
                 candidate["direction"], LIVE_VERSION,
                 display_name(candidate["symbol"], candidate.get("name"))),
            )
            db.commit()
            return ident if cursor.rowcount else None

    def sent(self, ident, when, message_id, content_sha=None):
        with closing(_connect(self.path)) as db:
            db.execute(
                "UPDATE signals SET sent_at=?,message_id=?,outcome='PENDING',card_sha=?"
                " WHERE id=? AND outcome='RESERVED'",
                (when, message_id, content_sha, ident),
            )
            db.commit()

    def not_delivered(self, ident):
        with closing(_connect(self.path)) as db:
            db.execute(
                "UPDATE signals SET outcome='NOT_DELIVERED' "
                "WHERE id=? AND outcome='RESERVED'",
                (ident,),
            )
            db.commit()

    def pending(self, now):
        with closing(_connect(self.path)) as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM signals WHERE sent_at IS NOT NULL AND "
                "(outcome='PENDING' OR "
                "(outcome='UNKNOWN' AND created_at>=?)) ORDER BY entry_at",
                (now - 24 * 3600,),
            )]

    def save_outcome(self, ident, attempts, sources, state, when):
        with closing(_connect(self.path)) as db:
            db.execute(
                "UPDATE signals SET attempts_json=?,attempt_sources_json=?,"
                "outcome=?,resolved_at=?,card_sha=NULL "
                "WHERE id=? AND sent_at IS NOT NULL",
                (json.dumps(attempts), json.dumps(sources), state, when, ident),
            )
            db.commit()

    def cards(self):
        with closing(_connect(self.path)) as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM signals WHERE message_id IS NOT NULL "
                "AND (outcome='PENDING' OR card_sha IS NULL) "
                "ORDER BY created_at DESC LIMIT 300",
            )]

    def card_edited(self, ident, signature):
        with closing(_connect(self.path)) as db:
            db.execute(
                "UPDATE signals SET card_sha=? WHERE id=?",
                (signature, ident),
            )
            db.commit()


def load_live_signals(path, since=0):
    """Only the 3 strategies actually delivered as prospective Telegram signals."""
    if not Path(path).is_file():
        return []
    from services.otc_public_stats import METHOD_LABELS, _attempts, _state

    with closing(_connect(path)) as db:
        raw = db.execute(
            "SELECT id,strategy,symbol,display_name,seconds,created_at,entry_at,expires_at,"
            "direction,version,sent_at,outcome,attempts_json,resolved_at,"
            "attempt_sources_json FROM signals WHERE sent_at IS NOT NULL "
            "AND sent_at>=? ORDER BY created_at,id", (since,),
        ).fetchall()
    rows = []
    for r in raw:
        row = dict(r)
        try:
            attempts = json.loads(row["attempts_json"] or "[]")
            sources = json.loads(row["attempt_sources_json"] or "[]")
        except ValueError:
            attempts, sources = [], []
        rows.append(dict(
            id=row["id"], origin="live", strategy=row["strategy"],
            version=row["version"], seconds=row["seconds"],
            label=METHOD_LABELS.get(row["strategy"], row["strategy"]),
            symbol=display_name(row["symbol"], row["display_name"]),
            direction=row["direction"],
            created_at=row["created_at"], sent_at=row["sent_at"],
            entry_at=row["entry_at"], expires_at=row["expires_at"],
            resolved_at=row["resolved_at"] or row["expires_at"],
            outcome=_state(row["outcome"]), attempts=_attempts(attempts),
            attempt_sources=sources,
            provenance="OTCharts · опубликованный прогноз (не исполнение брокера)",
        ))
    return rows


def candidate_list(path, now):
    """Read only newly observed eligible candidates from the current session."""
    if not Path(path).is_file():
        return []
    with closing(_connect(path)) as db:
        raw = db.execute(
            "SELECT payload FROM candidates WHERE observed_at>=? "
            "AND observed_at<=? AND entry_at>? AND entry_at<=? "
            "ORDER BY observed_at DESC,symbol",
            (now - 12, now, now + MIN_LEAD, now + MAX_LEAD),
        ).fetchall()
    result = []
    for item in raw:
        try:
            row = json.loads(item[0])
            exp = row.get("experiment") or {}
            if (
                exp.get("version") != RESEARCH_VERSION
                or exp.get("method") not in METHODS
                or exp.get("cycle_seconds") not in (120, 300, 900)
                or row.get("direction") not in ("UP", "DOWN")
                or not row.get("gates", {}).get("cycle")
                or not 90 <= float(row.get("payout") or 0) <= 100
                or not MIN_LEAD < float(row["entry_at"]) - now <= MAX_LEAD
                or row["observed_at"] >= row["entry_at"]
            ):
                continue
            result.append(row)
        except (ValueError, TypeError, KeyError):
            continue
    # At most one instrument per method, duration and entry slot, ranked
    # deterministically without looking at future WIN/LOSS outcomes.
    result.sort(key=lambda r: (
        r["experiment"]["method"], r["experiment"]["cycle_seconds"],
        r["entry_at"], -r["payout"], r["symbol"],
    ))
    unique = {}
    for row in result:
        key = (
            row["experiment"]["method"], row["experiment"]["cycle_seconds"],
            row["entry_at"],
        )
        unique.setdefault(key, row)
    return list(unique.values())


def _tick_outcome(prices, symbol, start, end, direction):
    start_price = prices.get((symbol, start))
    end_price = prices.get((symbol, end))
    if start_price is None or end_price is None:
        return None
    return direction_outcome(direction, start_price, end_price)


def _canonical_outcome(archive_path, symbol, start, end, direction):
    """Use only identical-minute, unrevised official historical opens."""
    if not Path(archive_path).is_file():
        return None
    with closing(_connect(archive_path)) as db:
        try:
            rows = db.execute(
                "SELECT time,open FROM bars WHERE symbol=? AND time IN (?,?) "
                "AND source='otcharts_history' AND revisions=0",
                (symbol, start, end),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
    prices = {int(r[0]): r[1] for r in rows
              if isinstance(r[1], (int, float)) and
              math.isfinite(r[1]) and r[1] > 0}
    if start not in prices or end not in prices:
        return None
    return direction_outcome(direction, prices[start], prices[end])


def evaluate_cycle(row, prices, now, archive_path=None):
    """Conservative prospective results; no hindsight invented attempts."""
    seconds = row["seconds"]
    attempts, sources = [], []
    for i in range(3):
        start = int(row["entry_at"] + i * seconds)
        end = start + seconds
        # The tick journal only records samples observed at/after their target.
        # An already-confirmed expiry sample can settle immediately; otherwise
        # allow time for the stream/history to supply the missing exact quote.
        if now < end:
            return attempts, sources, "PENDING"
        outcome = _tick_outcome(prices, row["symbol"], start, end, row["direction"])
        source = "otcharts_tick"
        if outcome is None and archive_path:
            outcome = _canonical_outcome(
                archive_path, row["symbol"], start, end, row["direction"]
            )
            source = "otcharts_history"
        if outcome is None:
            if now < end + RECOVERY_GRACE:
                return attempts, sources, "PENDING"
            return attempts + ["UNKNOWN"], sources + ["unconfirmed"], "UNKNOWN"
        attempts.append(outcome)
        sources.append(source)
        if outcome != "LOSS":
            return attempts, sources, outcome
    return attempts, sources, "LOSS"


def reconcile_unknown(row, prices, now, archive_path=None):
    """Recover the missing actual attempt without inventing later entries.

    After UNKNOWN, further entries were not instructed. Even if a historical
    quote later proves that attempt lost, do not retroactively fabricate
    attempts 2 or 3 and an imaginary total LOSS.
    """
    attempts = json.loads(row["attempts_json"] or "[]")
    sources = json.loads(row["attempt_sources_json"] or "[]")
    if "UNKNOWN" not in attempts:
        return attempts, sources, "UNKNOWN"
    index = attempts.index("UNKNOWN")
    seconds = row["seconds"]
    start = int(row["entry_at"] + index * seconds)
    end = start + seconds
    state = _tick_outcome(prices, row["symbol"], start, end, row["direction"])
    source = "otcharts_tick"
    if state is None and archive_path:
        state = _canonical_outcome(
            archive_path, row["symbol"], start, end, row["direction"]
        )
        source = "otcharts_history"
    if state is None:
        return attempts, sources, "UNKNOWN"
    attempts[index] = state
    if len(sources) > index:
        sources[index] = source
    else:
        sources.extend(["unconfirmed"] * (index - len(sources)))
        sources.append(source)
    if state in ("WIN", "DRAW"):
        return attempts, sources, state
    if index == 2:
        return attempts, sources, "LOSS"
    return attempts, sources, "UNKNOWN"


class LiveStrategyPublisher:
    def __init__(
        self, root, *, clock=time.time, journal=None, subscriber_relay=None
    ):
        self.root = Path(root)
        self.clock = clock
        self.journal = journal or LiveStrategyJournal(
            self.root / "data" / "community-live.db"
        )
        self.candidate_path = self.root / "data" / "community-cycles.db"
        self.archive_path = self.root / "data" / "market-history.db"
        self.started_at = self.clock()
        self._last_settlement = 0
        self.subscriber_relay = subscriber_relay
        self._diagnostic = {key: 0 for key in (
            "candidates", "timeframe", "stale_or_timing", "reserved_duplicate",
            "claim_error", "overlap", "late", "owner_error", "sent", "relay_error",
        )}
        self._diagnostic_at = self.clock()

    def _report_diagnostic(self):
        now = self.clock()
        if now - self._diagnostic_at < 300:
            return
        LOG.info("OTC S2-S4 delivery diagnostics (last %.0fs): %s",
                 now - self._diagnostic_at, self._diagnostic)
        self._diagnostic = {key: 0 for key in self._diagnostic}
        self._diagnostic_at = now

    async def publish(self, bot, owner, enabled=True, allowed_timeframes=(120, 300, 900)):
        if not enabled:
            return
        now = self.clock()
        rows = await asyncio.to_thread(candidate_list, self.candidate_path, now)
        self._diagnostic["candidates"] += len(rows)
        self._report_diagnostic()
        # Stable, explicitly configured tie-break, not a claimed accuracy score.
        rows.sort(key=lambda r: (
            STRATEGY_PRIORITY[r["experiment"]["method"]],
            -r["experiment"]["cycle_seconds"], -r["payout"], r["symbol"],
        ))
        for row in rows:
            now = self.clock()
            if row["experiment"]["cycle_seconds"] not in allowed_timeframes:
                self._diagnostic["timeframe"] += 1
                continue
            if (row["observed_at"] < self.started_at - 2
                    or not MIN_LEAD < row["entry_at"] - now <= SECONDARY_MAX_LEAD):
                self._diagnostic["stale_or_timing"] += 1
                continue
            ident = await asyncio.to_thread(self.journal.reserve, row, now)
            if not ident:
                self._diagnostic["reserved_duplicate"] += 1
                continue
            method, seconds = row["experiment"]["method"], row["experiment"]["cycle_seconds"]
            try:
                claimed = await asyncio.to_thread(
                    claim,
                    self.root / "data" / "signal-delivery.db",
                    row, method, self.clock(),
                    exclusive_timeframe=True,
                )
            except Exception as exc:
                self._diagnostic["claim_error"] += 1
                LOG.exception("Strategy claim failed")
                await asyncio.to_thread(self.journal.not_delivered, ident)
                continue
            if not claimed:
                self._diagnostic["overlap"] += 1
                await asyncio.to_thread(self.journal.not_delivered, ident)
                LOG.info("Overlapping OTC entry suppressed: %s %s", method, row["symbol"])
                continue
            if row["entry_at"] - self.clock() <= MIN_LEAD:
                self._diagnostic["late"] += 1
                await asyncio.to_thread(self.journal.not_delivered, ident)
                continue
            from services.otc_public_stats import METHOD_LABELS
            signal = dict(
                id=ident, origin="live", label=METHOD_LABELS[method],
                seconds=seconds,
                symbol=display_name(row["symbol"], row.get("name")),
                direction=row["direction"], entry_at=row["entry_at"],
                outcome="PENDING", attempts=[],
            )
            try:
                timeout = min(3, max(0.1, row["entry_at"] - MIN_LEAD - self.clock()))
                card_text = public_text(signal)
                message = await asyncio.wait_for(
                    bot.send_message(owner, card_text, parse_mode=None),
                    timeout,
                )
                when = self.clock()
                if row["entry_at"] - when <= MIN_LEAD:
                    self._diagnostic["late"] += 1
                    # A delivered late card is not counted as an actionable win.
                    await asyncio.to_thread(self.journal.not_delivered, ident)
                    try:
                        await bot.edit_message_text(
                            "⚠️ Сигнал опоздал — НЕ ВХОДИТЬ.\n"
                            + signal["label"] + " · " + signal["symbol"],
                            chat_id=owner, message_id=message.message_id,
                            parse_mode=None,
                        )
                    except Exception:
                        LOG.warning("Could not mark a late owner card")
                    continue
                await asyncio.to_thread(
                    self.journal.sent, ident, when, message.message_id,
                    hashlib.sha256(card_text.encode()).hexdigest()
                )
                self._diagnostic["sent"] += 1
                if self.subscriber_relay is not None:
                    try:
                        await self.subscriber_relay.publish_now(
                            bot,
                            dict(
                                signal,
                                created_at=now,
                                sent_at=when,
                                expires_at=row["expires_at"],
                                strategy=method,
                                version=row["experiment"].get("version", "unknown"),
                                resolved_at=row["expires_at"],
                            ),
                        )
                    except Exception:
                        self._diagnostic["relay_error"] += 1
                        LOG.exception("Strategy %s subscriber relay failed after owner delivery", method)
            except Exception:
                self._diagnostic["owner_error"] += 1
                LOG.exception("Strategy %s owner delivery failed", method)
                await asyncio.to_thread(self.journal.not_delivered, ident)

    async def settle(self):
        now = self.clock()
        if now - self._last_settlement < 2:
            return
        self._last_settlement = now
        pending = await asyncio.to_thread(self.journal.pending, now)
        if not pending:
            return
        since = min(r["entry_at"] for r in pending) - 2
        _, prices = await asyncio.to_thread(
            load, self.candidate_path, max(0, since), now
        )
        for row in pending:
            try:
                evaluator = (
                    reconcile_unknown if row["outcome"] == "UNKNOWN"
                    else evaluate_cycle
                )
                attempts, sources, state = await asyncio.to_thread(
                    evaluator, row, prices, now, self.archive_path
                )
                previous = json.loads(row["attempts_json"] or "[]")
                if attempts != previous or state != row["outcome"]:
                    await asyncio.to_thread(
                        self.journal.save_outcome, row["id"], attempts,
                        sources, state, now
                    )
                    if state != "PENDING" and attempts:
                        await asyncio.to_thread(
                            completed,
                            self.root / "data" / "signal-delivery.db",
                            row, row["strategy"],
                            row["entry_at"] + len(attempts) * row["seconds"],
                        )
            except Exception as exc:
                LOG.warning("Strategy settlement skipped: %s", type(exc).__name__)

    async def update_owner_cards(self, bot, owner):
        rows = await asyncio.to_thread(self.journal.cards)
        from services.otc_public_stats import METHOD_LABELS
        for row in rows:
            try:
                attempts = json.loads(row["attempts_json"] or "[]")
                signal = dict(
                    id=row["id"], origin="live",
                    label=METHOD_LABELS.get(row["strategy"], row["strategy"]),
                    seconds=row["seconds"],
                    symbol=display_name(row["symbol"], row.get("display_name")),
                    direction=row["direction"], entry_at=row["entry_at"],
                    outcome=row["outcome"], attempts=attempts,
                )
                content = public_text(signal)
                sig = hashlib.sha256(content.encode()).hexdigest()
                if sig == row["card_sha"]:
                    continue
                await bot.edit_message_text(
                    content, chat_id=owner, message_id=row["message_id"],
                    parse_mode=None,
                )
                await asyncio.to_thread(self.journal.card_edited, row["id"], sig)
            except Exception as exc:
                if "message is not modified" in str(exc).lower():
                    await asyncio.to_thread(
                        self.journal.card_edited, row["id"], sig
                    )
                else:
                    LOG.warning("Strategy card update: %s", type(exc).__name__)

    async def run(self, bot, owner, session=None, cycles=None):
        while True:
            try:
                enabled = bool(
                    session and session.running()
                    and cycles and getattr(cycles, "enabled", False)
                )
                allowed = tuple(getattr(cycles, "allowed_timeframes", (120, 300, 900)))
                await self.publish(bot, owner, enabled, allowed)
                await self.settle()
                await self.update_owner_cards(bot, owner)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Strategy publisher skipped tick: %s", type(exc).__name__)
            await asyncio.sleep(POLL_INTERVAL)
