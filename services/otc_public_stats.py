"""Independent, read-only OTC forecast statistics. Never reads wallet files.

Public results are OTCharts model outcomes, not broker-executed trades.
"""
import hashlib
import json
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

OUTCOMES = frozenset(("WIN", "LOSS", "DRAW", "UNKNOWN", "PENDING"))
METHOD_LABELS = {
    "bb_reentry": "Боллинджер · возврат",
    "break_retest": "Пробой и ретест",
    "ema_pullback": "Откат EMA9/19",
    "exhaustion": "Свой метод · Истощение и отскок",
}
MAIN_LABEL = "Боллинджер ×3"


def _db(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)


def _state(value):
    return value if value in OUTCOMES else "UNKNOWN"


def _attempts(values):
    """Actual attempts only; after WIN, DRAW or UNKNOWN remaining slots are cancelled."""
    attempts = []
    for value in values if isinstance(values, list) else ():
        if value not in OUTCOMES:
            value = "UNKNOWN"
        if value == "PENDING":
            break
        attempts.append(value)
        if value != "LOSS":
            break
    return attempts


def main_signals(path, since=0, *, include_cancelled=False):
    """Read only delivered Bollinger cycles; never call the private wallet."""
    if not Path(path).is_file():
        return []
    with closing(_db(path)) as db:
        db.row_factory = sqlite3.Row
        # Project only safe JSON subfields. Never SELECT/load the private
        # virtual-account segment embedded in older journal evidence.
        rows = db.execute(
            "SELECT id,symbol,created_at,entry_at,expires_at,direction,model_sha,"
            "CASE WHEN json_valid(evidence) THEN "
            "json_extract(evidence,'$.cycle_seconds') END AS cycle_seconds,"
            "CASE WHEN json_valid(evidence) THEN "
            "json_extract(evidence,'$.name') END AS display_name,"
            "CASE WHEN json_valid(evidence) THEN "
            "json_extract(evidence,'$.attempt_outcomes') END AS attempts_json,"
            "sent_at,outcome,resolved_at FROM predictions "
            "WHERE sent_at IS NOT NULL AND sent_at >= ? "
            "AND (outcome NOT IN ('LATE','CANCELLED','NOT_DELIVERED') "
            "OR (?=1 AND outcome='CANCELLED')) "
            "ORDER BY created_at,id", (since, int(include_cancelled))
        ).fetchall()
    result = []
    for raw in rows:
        row = dict(raw)
        seconds = row["cycle_seconds"]
        if seconds not in (60, 120, 300, 900):
            continue
        try:
            attempts = json.loads(row["attempts_json"] or "[]")
        except (TypeError, ValueError):
            attempts = []
        result.append(dict(
            id="main:" + str(row["id"]),
            origin="main",
            strategy="bollinger_live",
            version=row["model_sha"] or "unknown",
            label=MAIN_LABEL,
            seconds=seconds,
            symbol=str(row["display_name"] or row["symbol"])[:90],
            direction=row["direction"] if row["direction"] in ("UP", "DOWN") else "?",
            created_at=float(row["created_at"]),
            sent_at=float(row["sent_at"]),
            entry_at=float(row["entry_at"]),
            expires_at=float(row["expires_at"]),
            resolved_at=float(row["resolved_at"] or row["expires_at"]),
            outcome="CANCELLED" if include_cancelled and row["outcome"] == "CANCELLED" else _state(row["outcome"]),
            attempts=_attempts(attempts),
            provenance="OTCharts · сохранённый цикл; не подтверждено брокером",
        ))
    return result


def shadow_signals(path, since=0, now=None):
    """Research candidates, never presented as actually delivered signals."""
    from services.otc_community_cycles import METHODS, VERSION
    from services.otc_validation_report import load, resolve

    now = time.time() if now is None else now
    path = Path(path)
    if not path.exists():
        return []
    rows, prices = load(path, since, now)
    result = []
    for row in rows:
        trial = row.get("experiment") or {}
        method, seconds = trial.get("method"), trial.get("cycle_seconds")
        if trial.get("version") != VERSION or method not in METHODS:
            continue
        if seconds not in (60, 120, 300, 900):
            continue
        attempts = []
        for i in range(3):
            attempt = dict(
                row, entry_at=row["entry_at"] + i * seconds,
                expires_at=row["entry_at"] + (i + 1) * seconds,
            )
            state = _state(resolve(attempt, prices, now))
            if state == "PENDING":
                break
            attempts.append(state)
            if state != "LOSS":
                break
        outcome = (
            "PENDING" if len(attempts) < 3 and (not attempts or attempts[-1] == "LOSS")
            else attempts[-1] if attempts else "PENDING"
        )
        ident = hashlib.sha256(
            json.dumps(
                [method, seconds, row["symbol"], row["entry_at"], row["direction"]],
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
        result.append(dict(
            id="shadow:" + ident,
            origin="shadow",
            strategy=method,
            version=row.get("version") or "unknown",
            label=METHOD_LABELS[method] + " · ТЕСТ",
            seconds=seconds,
            symbol=str(row.get("symbol", ""))[:90],
            direction=row["direction"],
            created_at=float(row["observed_at"]),
            sent_at=None,
            entry_at=float(row["entry_at"]),
            expires_at=float(row["expires_at"]),
            resolved_at=float(row["expires_at"]),
            outcome=outcome,
            attempts=attempts,
            provenance="OTCharts · исследовательский прогноз, не отправленная сделка",
        ))
    return result


def combined_cycles(signals, now=None, wait_seconds=7200):
    """Deterministic chronological pairing. Second forecast starts AFTER first LOSS."""
    now = time.time() if now is None else now
    grouped = defaultdict(list)
    for row in signals:
        grouped[(row["origin"], row["strategy"], row["version"], row["seconds"])].append(row)
    result = []
    for group, rows in grouped.items():
        waiting = []
        for row in sorted(rows, key=lambda r: (r["created_at"], r["id"])):
            start = row["created_at"]
            for first in tuple(waiting):
                if start > first["resolved_at"] + wait_seconds:
                    result.append(_cycle([first], "INCOMPLETE"))
                    waiting.remove(first)
            candidate = next(
                (first for first in waiting
                 if start >= first["resolved_at"] and
                 start <= first["resolved_at"] + wait_seconds),
                None,
            )
            if candidate is not None:
                waiting.remove(candidate)
                result.append(_cycle([candidate, row], row["outcome"]))
            elif row["outcome"] == "LOSS":
                waiting.append(row)
            else:
                result.append(_cycle([row], row["outcome"]))
        for first in waiting:
            end = first["resolved_at"] + wait_seconds
            result.append(_cycle(
                [first], "INCOMPLETE" if now > end else "WAIT_SECOND"
            ))
    return sorted(result, key=lambda r: (r["created_at"], r["id"]))


def _cycle(rows, state):
    last = rows[-1]
    return dict(
        id="x6:" + ":".join(row["id"] for row in rows),
        origin=last["origin"],
        strategy=last["strategy"],
        version=last["version"],
        seconds=last["seconds"],
        label=last["label"],
        symbol=" + ".join(row["symbol"] for row in rows),
        created_at=rows[0]["created_at"],
        resolved_at=last["resolved_at"],
        outcome=state,
        attempts_count=sum(len(row.get("attempts") or []) for row in rows),
        parts=len(rows),
    )


def metric_units(signals, metric, now=None):
    if metric == "C":
        return combined_cycles(signals, now)
    output = []
    for row in signals:
        if metric == "B":
            output.append(row)
        elif metric == "A":
            for i, state in enumerate(row.get("attempts", [])):
                if state == "PENDING":
                    continue
                output.append(dict(
                    row, id=row["id"] + ":" + str(i + 1),
                    outcome=state,
                    resolved_at=row["entry_at"] + (i + 1) * row["seconds"],
                    symbol=row["symbol"] + " · попытка " + str(i + 1),
                ))
        else:
            raise ValueError("Unknown metric")
    return sorted(output, key=lambda r: (r["resolved_at"], r["id"]))


def metrics(rows):
    counts = Counter(row["outcome"] for row in rows)
    known = counts["WIN"] + counts["LOSS"]
    return dict(
        total=len(rows), known=known, win=counts["WIN"], loss=counts["LOSS"],
        draw=counts["DRAW"], unknown=counts["UNKNOWN"],
        pending=counts["PENDING"] + counts["WAIT_SECOND"],
        incomplete=counts["INCOMPLETE"],
        wr=100 * counts["WIN"] / known if known else None,
    )


def describe(group, metric, seconds, since=0, limit=10, now=None):
    rows = [
        row for row in metric_units(group, metric, now)
        if (seconds is None or row["seconds"] == seconds)
        and row["created_at"] >= since
    ]
    # Never silently merge different strategy versions or shadow with real delivery.
    rows.sort(key=lambda r: (r["resolved_at"], r["id"]))
    m = metrics(rows)
    title = {"A": "Попытки", "B": "Сигналы ×3", "C": "Объединённые циклы ×6"}[metric]
    expiry = "все экспирации" if seconds is None else "M" + str(seconds // 60)
    wr = f"{m['wr']:.1f}%" if m["wr"] is not None else "—"
    lines = [
        f"📊 {title} · {expiry}",
        f"WIN {m['win']} · LOSS {m['loss']} · WR {wr}",
        f"Всего {m['total']} · известных {m['known']} · ничьи {m['draw']}",
        f"UNKNOWN {m['unknown']} · ожидают {m['pending']} · без пары {m['incomplete']}",
        f"Охват подтверждённых WIN/LOSS: {m['known']}/{m['total']} "
        + (f"({100*m['known']/m['total']:.1f}%)" if m["total"] else "(—)"),
        "OTCharts, модельные исходы · не сделки брокера",
    ]
    if rows:
        lines.append("")
        for i, row in enumerate(rows[-max(1, min(limit, 100)):], 1):
            icon = {"WIN": "✅", "LOSS": "❌", "DRAW": "➖", "UNKNOWN": "❔"}.get(row["outcome"], "⏳")
            from datetime import datetime
            from zoneinfo import ZoneInfo
            local = datetime.fromtimestamp(
                row["created_at"], ZoneInfo("Europe/Moscow")
            ).strftime("%d.%m %H:%M")
            lines.append(f"{i}. {local} · {row['symbol']} — {icon}")
    return "\n".join(lines)
