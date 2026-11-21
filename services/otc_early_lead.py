"""Prospective 30-40s OTC timing experiment, strictly shadow-only.

No Telegram messages, orders, API requests, trading claims, or live journal writes.
The existing S1 and S2-S4 signal functions and data gates are reused unchanged.
"""
import argparse
import asyncio
import json
import logging
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from services.otc_candidate_research import CandidateJournal, TickSampler, finite_payload
from services.otc_community_cycles import signals as community_signals
from services.otc_delivery_guard import check_data
from services.otc_hourly_shadow import (
    H1_SECONDS, H1_VERSION, H1_WINDOW, hourly_candidate,
    hourly_entry, hourly_outcome,
)
from services.otc_groq import fresh
from services.otc_ml.preview import aligned_entry
from services.otc_shadow_variants import pattern_directions
from services.otc_strategies_live import evaluate_cycle
from services.otc_validation_report import load

LOG = logging.getLogger(__name__)
VERSION = "early-lead-30-40-v1"
WINDOW = (30, 40)
METHODS = {"bb_reentry": "S2", "break_retest": "S3", "ema_pullback": "S4"}
STRATEGIES = ("S1", "S2", "S3", "S4")
PERIODS = (120, 300, 900)


def candidate_rows(snapshot, now, allowed=PERIODS):
    """Generate only from data that existed when the early observation occurred.

    Both early and live modes use the same entry minute and feature cutoff.
    Never shift the input timestamp forward to pretend the next candle closed.
    """
    if snapshot is None:
        return []
    histories, assets, quotes, end = snapshot
    entry = int(now) // 60 * 60 + 60
    lead = entry - now
    if not WINDOW[0] <= lead <= WINDOW[1]:
        return []
    ranked = {}
    for seconds in allowed:
        if seconds not in PERIODS or not aligned_entry(entry, seconds):
            continue
        if end and entry + 3 * seconds + 2 > end:
            continue
        for symbol, minutes in histories.items():
            asset = assets.get(symbol)
            if not fresh(asset, quotes.get(symbol), now):
                continue
            base = dict(
                symbol=symbol, name=asset.name or symbol,
                entry_at=entry, expires_at=entry + 3 * seconds,
                cycle_seconds=seconds, feature_asof=entry - 60,
                observed_at=now, payout=asset.payout, gates={"cycle": True},
            )
            if check_data(base, snapshot, now):
                continue
            # S1 and S2-S4 use the exact same pure forecast functions as live.
            s1 = pattern_directions(list(minutes), seconds, entry - 60).get(
                "bollinger_reversal"
            )
            choices = {"S1": s1} if s1 else {}
            choices.update({
                METHODS[method]: decision
                for method, decision in community_signals(
                    list(minutes), seconds, entry - 60
                ).items() if method in METHODS
            })
            for strategy, pattern in choices.items():
                if pattern["direction"] not in ("UP", "DOWN"):
                    continue
                row = dict(
                    base, version=VERSION + ":" + strategy,
                    direction=pattern["direction"],
                    experiment=dict(
                        version=VERSION, strategy=strategy,
                        cycle_seconds=seconds, lead_window=list(WINDOW),
                        shadow_only=True,
                    ),
                )
                key = strategy, seconds, entry
                # Deterministic candidate selection matches live's payout
                # ranking, before either timing arm's own cycle exclusions.
                priority = (-asset.payout, symbol)
                if key not in ranked or priority < ranked[key][0]:
                    ranked[key] = (priority, row)
    return [item[1] for key, item in sorted(ranked.items())]


class EarlyJournal:
    """Separate SQLite store. Existing forecasting journals remain untouched."""

    def __init__(self, path):
        self.path = Path(path)
        self.candidates = CandidateJournal(self.path)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS early_slots(
                    strategy TEXT NOT NULL,seconds INTEGER NOT NULL,
                    entry_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,
                    symbol TEXT NOT NULL,direction TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    PRIMARY KEY(strategy,seconds,entry_at)
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS early_lock "
                "ON early_slots(strategy,seconds,entry_at,expires_at)"
            )
            db.commit()

    def record(self, rows):
        accepted = []
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute("BEGIN IMMEDIATE")
            for row in rows:
                strategy = row["experiment"]["strategy"]
                seconds = int(row["cycle_seconds"])
                entry = int(row["entry_at"])
                expires = int(row["expires_at"])
                if db.execute(
                    "SELECT 1 FROM early_slots WHERE strategy=? AND seconds=? "
                    "AND entry_at<? AND expires_at>? LIMIT 1",
                    (strategy, seconds, expires, entry),
                ).fetchone():
                    continue
                inserted = db.execute(
                    "INSERT OR IGNORE INTO early_slots VALUES(?,?,?,?,?,?,?)",
                    (strategy, seconds, entry, expires, row["symbol"],
                     row["direction"], row["observed_at"]),
                ).rowcount
                if not inserted:
                    continue
                db.execute(
                    "INSERT INTO candidates "
                    "(version,symbol,entry_at,expires_at,observed_at,payload) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        row["version"], row["symbol"], entry, expires,
                        row["observed_at"],
                        json.dumps(finite_payload(row), ensure_ascii=False,
                                   allow_nan=False),
                    ),
                )
                accepted.append(row)
            db.commit()
        return accepted

    def pending_future(self, now):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            rows = db.execute(
                "SELECT payload FROM candidates WHERE expires_at>? "
                "AND observed_at<entry_at ORDER BY entry_at", (now,),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]


class EarlyLeadObserver:
    def __init__(self, root, clock=time.time):
        self.root = Path(root)
        self.clock = clock
        self.journal = EarlyJournal(self.root / "data" / "early-lead-30-40.db")
        self.sampler = TickSampler()
        self.snapshot = None
        self.session_end = None
        self.last_plan_at = None
        self.last_hourly_plan_at = None
        self.hourly_recorded_at = None
        self.next_flush = 0
        self.counters = Counter()
        self.next_report = clock() + 300
        self.sampler.register(self.journal.pending_future(self.clock()), self.clock())

    def observe(self, history, assets, quotes, end):
        # A separate snapshot of the *same* provider feed, not a new stream.
        self.snapshot = (history, assets, quotes, end)

    def observe_tick(self, tick, now):
        self.sampler.observe(tick, now)

    async def flush(self):
        data = self.sampler.drain(self.clock())
        if data:
            try:
                await asyncio.to_thread(self.journal.candidates.samples, data)
            except Exception:
                self.sampler.restore(data)
                raise

    async def tick_hourly(self, now):
        """Prospective H1 scan at 5 minutes before the next UTC hour.

        Shadow-only and entirely independent from the M2/M5/M15 planner.
        """
        entry = hourly_entry(now)
        if not H1_WINDOW[0] <= entry - now <= H1_WINDOW[1]:
            return
        if self.hourly_recorded_at == entry:
            return
        if (self.last_hourly_plan_at is not None
                and self.last_hourly_plan_at[0] == entry
                and now - self.last_hourly_plan_at[1] < 3):
            return
        self.last_hourly_plan_at = (entry, now)
        history, assets, quotes, end = self.snapshot
        frozen = ({s: list(b) for s, b in history.items()},
                  dict(assets), dict(quotes), end)
        row = await asyncio.to_thread(
            hourly_candidate, frozen, now,
            self.root / "data" / "market-history.db",
        )
        actual = self.clock()
        if not H1_WINDOW[0] <= entry - actual <= H1_WINDOW[1]:
            self.counters["hourly_too_late"] += 1
            return
        if row is None:
            self.counters["hourly_no_confirmed_pattern_or_history"] += 1
            return
        if check_data(row, frozen, actual) is not None:
            self.counters["hourly_stale_before_record"] += 1
            return
        recorded = await asyncio.to_thread(self.journal.record, [row])
        self.sampler.register(recorded, self.clock())
        self.counters["hourly_recorded"] += len(recorded)
        if recorded:
            self.hourly_recorded_at = entry
            LOG.info(
                "OTC H1 shadow forecast: entry=%d, %.1fs lead, source=%s; "
                "single 60-minute attempt; no Telegram delivery",
                entry, entry - now, row["experiment"]["source_strategy"],
            )

    async def tick(self, session, cycles):
        now = self.clock()
        if now >= self.next_flush:
            self.next_flush = now + 2
            await self.flush()
        if now >= self.next_report:
            LOG.info("OTC early 30-40s shadow diagnostics (last 300s): %s",
                     dict(self.counters))
            self.counters.clear()
            self.next_report = now + 300
        if not (session.running() and getattr(cycles, "enabled", False)
                and self.snapshot is not None and session.transport == "live"):
            return
        await self.tick_hourly(now)
        entry = int(now) // 60 * 60 + 60
        if not WINDOW[0] <= entry - now <= WINDOW[1]:
            return
        # Give transiently stale quotes a few seconds to recover inside the
        # same early window. Up to four scans, never a backfilled forecast.
        if (self.last_plan_at is not None
                and self.last_plan_at[0] == entry
                and now - self.last_plan_at[1] < 3):
            return
        self.last_plan_at = (entry, now)
        history, assets, quotes, end = self.snapshot
        frozen = ({s: list(b) for s, b in history.items()},
                  dict(assets), dict(quotes), end)
        allowed = tuple(getattr(cycles, "allowed_timeframes", PERIODS))
        rows = await asyncio.to_thread(candidate_rows, frozen, now, allowed)
        actual = self.clock()
        if not WINDOW[0] <= entry - actual <= WINDOW[1]:
            self.counters["late_computation"] += 1
            return
        # Like the live sender, reject a candidate if its previously observed
        # quote/payout expired while computing; never relax timing or freshness.
        confirmed = [row for row in rows if check_data(row, frozen, actual) is None]
        recorded = await asyncio.to_thread(self.journal.record, confirmed)
        self.sampler.register(recorded, self.clock())
        self.counters["planning_windows"] += 1
        self.counters["candidates"] += len(rows)
        self.counters["rejected_before_record"] += len(rows) - len(confirmed)
        self.counters["recorded"] += len(recorded)
        if rows:
            LOG.info("OTC early 30-40s shadow: entry=%d lead=%.1fs "
                     "candidates=%d recorded=%d (no Telegram delivery)",
                     entry, entry - now, len(rows), len(recorded))

    async def run(self, session, cycles):
        try:
            while True:
                try:
                    await self.tick(session, cycles)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.warning("OTC early shadow skipped: %s", type(exc).__name__)
                await asyncio.sleep(1)
        finally:
            try:
                await self.flush()
            except Exception:
                LOG.exception("OTC early shadow final flush failed")


def _live_rows(root, since):
    """Delivered live signals only; never include undelivered research candidates."""
    result = []
    root = Path(root)
    main = root / "data" / "bollinger-cycles.db"
    if main.is_file():
        with closing(sqlite3.connect(main.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            for symbol, entry, seconds, direction, outcome, sent_at in db.execute(
                "SELECT symbol,entry_at,"
                "CASE WHEN json_valid(evidence) THEN "
                "json_extract(evidence,'$.cycle_seconds') END,"
                "direction,outcome,sent_at FROM predictions "
                "WHERE sent_at>=? AND outcome NOT IN "
                "('NOT_DELIVERED','LATE','CANCELLED')", (since,),
            ):
                if seconds in PERIODS:
                    result.append(("S1", symbol, int(seconds), int(entry),
                                   direction, outcome, float(sent_at)))
    others = root / "data" / "community-live.db"
    if others.is_file():
        with closing(sqlite3.connect(others.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            for method, symbol, seconds, entry, direction, outcome, sent_at in db.execute(
                "SELECT strategy,symbol,seconds,entry_at,direction,outcome,sent_at "
                "FROM signals WHERE sent_at>=? AND outcome NOT IN "
                "('NOT_DELIVERED','LATE','CANCELLED','RESERVED')", (since,),
            ):
                strategy = METHODS.get(method)
                if strategy and seconds in PERIODS:
                    result.append((strategy, symbol, seconds, int(entry),
                                   direction, outcome, float(sent_at)))
    return result


def comparison(root, since=0, now=None):
    """Read-only report; matched forecasts compare identical market entry slots."""
    root = Path(root)
    now = time.time() if now is None else now
    path = root / "data" / "early-lead-30-40.db"
    rows, prices = load(path, since, now)
    early = {}
    for row in rows:
        exp = row.get("experiment") or {}
        strategy, seconds = exp.get("strategy"), exp.get("cycle_seconds")
        if exp.get("version") != VERSION or strategy not in STRATEGIES:
            continue
        if not isinstance(seconds, int) or seconds not in PERIODS:
            continue
        key = strategy, row["symbol"], seconds, int(row["entry_at"])
        _, _, outcome = evaluate_cycle(
            dict(row, seconds=seconds), prices, now,
            root / "data" / "market-history.db"
        )
        early[key] = (row["direction"], outcome, row["observed_at"])
    # Don't inflate the baseline with live signals from before the experiment.
    # The common observation window starts at the first recorded early forecast.
    common_since = max(since, min((v[2] for v in early.values()), default=now))
    live = {
        (strategy, symbol, seconds, entry): (direction, outcome, sent_at)
        for strategy, symbol, seconds, entry, direction, outcome, sent_at
        in _live_rows(root, common_since)
    }
    groups = []
    for strategy in STRATEGIES:
        for seconds in PERIODS:
            e = {k: v for k, v in early.items()
                 if k[0] == strategy and k[2] == seconds}
            l = {k: v for k, v in live.items()
                 if k[0] == strategy and k[2] == seconds}
            shared = e.keys() & l.keys()
            agree = sum(e[k][0] == l[k][0] for k in shared)
            e_counts = Counter(v[1] for v in e.values())
            l_counts = Counter(v[1] for v in l.values())
            both_known = [k for k in shared
                          if e[k][1] in ("WIN", "LOSS") and
                          l[k][1] in ("WIN", "LOSS")]
            groups.append(dict(
                strategy=strategy, seconds=seconds,
                early=len(e), live=len(l), matched=len(shared),
                early_lead_seconds=(
                    round(sum(k[3] - v[2] for k, v in e.items()) / len(e), 1)
                    if e else None
                ),
                live_lead_seconds=(
                    round(sum(k[3] - v[2] for k, v in l.items()) / len(l), 1)
                    if l else None
                ),
                directions_agree=agree,
                early_outcomes=dict(e_counts), live_outcomes=dict(l_counts),
                paired_known=len(both_known),
                early_paired_win=sum(e[k][1] == "WIN" for k in both_known),
                live_paired_win=sum(l[k][1] == "WIN" for k in both_known),
            ))
    return groups


def early_winrate(root, since=0, now=None):
    """Independent early-only cycle winrate; never reads delivered signal journals.

    Results are evaluated prospectively from the separate shadow journal.
    Only confirmed WIN and LOSS count toward WR. Unknown, draw and pending
    outcomes are visible, not silently counted as wins or losses.
    """
    root = Path(root)
    now = time.time() if now is None else now
    rows, prices = load(root / "data" / "early-lead-30-40.db", since, now)
    by_strategy = {strategy: Counter() for strategy in STRATEGIES}
    by_timeframe = {
        (strategy, seconds): Counter()
        for strategy in STRATEGIES for seconds in PERIODS
    }
    overall = Counter()
    hourly = Counter()
    hourly_sources = Counter()
    first_at = last_at = None
    for row in rows:
        experiment = row.get("experiment") or {}
        strategy = experiment.get("strategy")
        seconds = experiment.get("cycle_seconds")
        if (experiment.get("version") == H1_VERSION
                and strategy == "H1"
                and seconds == H1_SECONDS
                and experiment.get("attempts") == 1
                and row.get("direction") in ("UP", "DOWN")
                and H1_WINDOW[0] <= row["entry_at"] - row["observed_at"] <= H1_WINDOW[1]):
            status = hourly_outcome(
                row, prices, now, root / "data" / "market-history.db"
            )
            hourly[status] += 1
            hourly_sources[experiment.get("source_strategy", "?")] += 1
            continue
        if (experiment.get("version") != VERSION
                or strategy not in STRATEGIES
                or seconds not in PERIODS
                or row.get("direction") not in ("UP", "DOWN")):
            continue
        # Report what was actually recorded at the earlier time, never
        # infer a forecast retrospectively from later market data.
        recorded = float(row["observed_at"])
        lead = float(row["entry_at"]) - recorded
        if not WINDOW[0] <= lead <= WINDOW[1]:
            continue
        first_at = min(first_at, recorded) if first_at is not None else recorded
        last_at = max(last_at, recorded) if last_at is not None else recorded
        _, _, outcome = evaluate_cycle(
            dict(row, seconds=seconds), prices, now,
            root / "data" / "market-history.db",
        )
        if outcome not in ("WIN", "LOSS", "DRAW", "PENDING", "UNKNOWN"):
            outcome = "UNKNOWN"
        overall[outcome] += 1
        by_strategy[strategy][outcome] += 1
        by_timeframe[(strategy, seconds)][outcome] += 1
    return dict(
        overall=overall, by_strategy=by_strategy, by_timeframe=by_timeframe,
        hourly=hourly, hourly_sources=hourly_sources,
        first_at=first_at, last_at=last_at, window=WINDOW,
    )


def format_early_winrate(stats, period="7 дней"):
    """Compact Telegram-safe owner report separated from main signal stats."""
    def record_text(counts):
        wins, losses = counts["WIN"], counts["LOSS"]
        known = wins + losses
        winrate = f"{100 * wins / known:.1f}%" if known else "—"
        counts_text = f"✅ {wins} / ❌ {losses} · WR {winrate}"
        others = (f" · ⏳ {counts['PENDING']} · ? {counts['UNKNOWN']}"
                  f" · = {counts['DRAW']}")
        return counts_text + others

    overall = stats["overall"]
    lines = [
        "🧪 ФОНОВЫЙ WR · M2/M5/M15 за 30–40 с + H1 за 5 мин",
        f"Период: {period}",
        "Только сохранённые фоновые циклы, НЕ отправленные подписчикам.",
        "",
        f"Всего прогнозов: {sum(overall.values())}",
        record_text(overall),
    ]
    if not sum(overall.values()):
        lines += [
            "",
            "Пока нет прогнозов M2/M5/M15 за выбранный период.",
            "На устройстве включи OTC_EARLY_SHADOW_ENABLED=true,",
            "перезапусти бот и запусти обычный OTC-сеанс.",
        ]
    else:
        for strategy in STRATEGIES:
            subtotal = stats["by_strategy"][strategy]
            lines.extend(["", f"**{strategy}** · прогнозов {sum(subtotal.values())}",
                          record_text(subtotal)])
            for seconds in PERIODS:
                counts = stats["by_timeframe"][(strategy, seconds)]
                lines.append(f"  M{seconds // 60}: {record_text(counts)}")
    lines.extend([
        "",
        "🕐 H1 · отдельный фоновый винрейт",
        f"Выдано H1: {sum(stats['hourly'].values())} · {record_text(stats['hourly'])}",
        "Прогноз за 5 мин до новой часовой свечи · 1 попытка.",
        *(
            ["H1 пока 0: нужна непрерывная история 20–60 часов, "
             "свежие данные и активная сессия перед началом часа."]
            if not sum(stats["hourly"].values()) else []
        ),
        "",
        "WR = WIN / (WIN + LOSS). DRAW, UNKNOWN, PENDING не учитываются.",
        "Это виртуальные исходы по OTCharts, а не подтверждённые сделки брокера.",
        "Основной винрейт S1–S4 не затронут.",
    ])
    return "\n".join(lines).replace("**", "")


def format_compact_report(groups):
    labels = {"bollinger_live": "S1", "main": "S1", "bb_reentry": "S2",
              "break_retest": "S3", "ema_pullback": "S4"}
    lines = ["⚖️ Сравнение · 7 дней", "Стратегия · фон WR (N) / основной WR (N)", ""]
    def rate(counts):
        wins, losses = counts.get("WIN", 0), counts.get("LOSS", 0)
        total = wins + losses
        return f"{100 * wins / total:.1f}% ({total})" if total else "— (0)"
    for group in groups:
        label = labels.get(group["strategy"], group["strategy"])
        lines.append(f"{label} M{group['seconds']//60} · {rate(group['early_outcomes'])} / {rate(group['live_outcomes'])}")
    if not groups:
        lines.append("Пока нет данных.")
    lines.extend(["", "N — завершённые WIN/LOSS циклы. Выборки могут различаться."])
    return "\n".join(lines)


def format_report(groups):
    lines = [
        "Сравнение входа: ранний 30–40 с против S1–S4 за 15–20 с (история — по фактической доставке)",
        "Ранние: только фоновые прогнозы; основные: только доставленные сигналы.",
        "Период сравнения начинается не раньше первого раннего прогноза.",
        "Это виртуальные результаты OTCharts, не сделки Pocket Option.",
        "WINRATE = WIN/(WIN+LOSS), PENDING/DRAW/UNKNOWN исключены.",
        "",
    ]
    for g in groups:
        e, l = g["early_outcomes"], g["live_outcomes"]
        def rate(c):
            wins, losses = c.get("WIN", 0), c.get("LOSS", 0)
            return f"{100*wins/(wins+losses):.1f}%" if wins+losses else "—"
        n = g["paired_known"]
        matched = (
            f"{g['early_paired_win']}/{n} vs {g['live_paired_win']}/{n}"
            if n else "нет подтверждённых пар"
        )
        lines.append(
            f"{g['strategy']} M{g['seconds']//60}: "
            f"ранних {g['early']} (WR {rate(e)}, "
            f"средний запас {g['early_lead_seconds'] if g['early_lead_seconds'] is not None else '—'}с), "
            f"основных {g['live']} (WR {rate(l)}, "
            f"запас {g['live_lead_seconds'] if g['live_lead_seconds'] is not None else '—'}с); "
            f"совпадений по паре/времени {g['matched']}, "
            f"одно направление {g['directions_agree']}; "
            f"WIN по сравнимым исходам {matched}"
        )
    lines += [
        "",
        "Ранний прогноз не является отправленным сигналом. "
        "Две группы могут выбирать разные пары и иметь разный размер выборки.",
        "Нельзя считать ранний вариант точнее, пока нет достаточных "
        "сопоставимых подтверждённых наблюдений.",
    ]
    return "\n".join(lines)


def main():
    from config.settings import ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    if not args.report or not 1 <= args.days <= 365:
        parser.error("Запуск отчёта: --report --days 7")
    print(format_report(comparison(ROOT, time.time() - args.days * 86400)))


if __name__ == "__main__":
    main()
