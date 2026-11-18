"""Conservative stream warm-up and persistent observed downtime intervals."""

import logging
import sqlite3
from collections import Counter
from contextlib import closing

from services.otc_delivery_guard import check_data

LOG = logging.getLogger(__name__)


class DataReadiness:
    def __init__(self, path, now):
        self.path = path
        self.ticks = {}
        self._journal_retry_at = 0
        self._diag = Counter()
        self._diag_since = now
        self._diag_last_reason = {}
        with closing(sqlite3.connect(path, timeout=1)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS gaps(start REAL, end REAL, reason TEXT)"
            )
            db.commit()
        self.reset(now, "startup")

    def reset(self, now, reason):
        self._diag[('reset', 'system', reason)] += 1
        self.ticks.clear()
        self.gap_open = True
        if now < self._journal_retry_at:
            return
        try:
            with closing(sqlite3.connect(self.path, timeout=0.1)) as db:
                if not db.execute("SELECT 1 FROM gaps WHERE end IS NULL").fetchone():
                    db.execute("INSERT INTO gaps VALUES(?,NULL,?)", (now, reason))
                db.commit()
        except sqlite3.OperationalError as exc:
            # Gap history is diagnostic, not source evidence. Never let this
            # optional write tear down the live market feed or bypass warmup.
            LOG.warning("OTC readiness journal reset deferred (%s)", type(exc).__name__)
            self._journal_retry_at = now + 10

    def observe(self, tick, now, history, assets, quotes):
        """Only report gate decisions; do not relax market-data validation."""
        previous = self.ticks.get(tick.symbol)
        if previous is None or now - previous[1] > 8:
            self.ticks[tick.symbol] = (now, now, 1)
        elif now > previous[1]:
            self.ticks[tick.symbol] = (previous[0], now, previous[2] + 1)
        ready = {}
        for symbol, (start, last, count) in self.ticks.items():
            if count < 3 or now - start < 10:
                self._diag[("not_ready", symbol, "warmup")] += 1
                self._diag_last_reason[symbol] = "warmup"
                continue
            if now - last > 8:
                self._diag[("not_ready", symbol, "last_tick_stale")] += 1
                self._diag_last_reason[symbol] = "last_tick_stale"
                continue
            selected = dict(symbol=symbol, feature_asof=now, expires_at=now + 300)
            reason = check_data(selected, (history, assets, quotes, None), now)
            if reason is None:
                ready[symbol] = quotes[symbol]
                self._diag[("ready", symbol, "ok")] += 1
                self._diag_last_reason[symbol] = "ok"
            else:
                self._diag[("not_ready", symbol, reason)] += 1
                self._diag_last_reason[symbol] = str(reason)
        if ready and self.gap_open and now >= self._journal_retry_at:
            try:
                with closing(sqlite3.connect(self.path, timeout=0.1)) as db:
                    db.execute("UPDATE gaps SET end=? WHERE end IS NULL", (now,))
                    db.commit()
                self.gap_open = False
            except sqlite3.OperationalError as exc:
                LOG.warning("OTC readiness journal close deferred (%s)", type(exc).__name__)
                self._journal_retry_at = now + 10

        if now - self._diag_since >= 300:
            reasons = {}
            for (status, symbol, reason), count in self._diag.items():
                key = symbol if symbol != "" else "system"
                reasons.setdefault(key, {})[status + ":" + reason] = count
            LOG.info(
                "OTC readiness gate diagnostics (last %.0fs): %s; last=%s",
                now - self._diag_since, reasons, self._diag_last_reason,
            )
            self._diag.clear()
            self._diag_since = now
        return ready


def downtime(path, since, now):
    if not path.exists():
        return dict(intervals=0, seconds=0, ongoing=False)
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute(
            "SELECT start,end FROM gaps WHERE start<? AND (end IS NULL OR end>?)",
            (now, since),
        ).fetchall()
    return dict(
        intervals=len(rows),
        seconds=sum(
            max(0, min(end or now, now) - max(start, since)) for start, end in rows
        ),
        ongoing=any(end is None for _, end in rows),
    )
