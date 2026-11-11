"""Audited correction of advice-dependent outcomes; never infer wins without exact bars."""

import json
import sqlite3
import time
from contextlib import closing

from services.otc_evaluation import assess

LEGACY = (
    "entry_quality_late",
    "entry_quality_rejected",
    "entry_check_missing_data",
    "entry_check_cancelled",
)
VERSION = "published-condition-v1"


async def correct_advisory_outcomes(db, provider, now=None):
    now = time.time() if now is None else now
    with db.connect() as connection:
        rows = [
            dict(r)
            for r in connection.execute(
                """
            SELECT p.*,a.outcome old_outcome,a.reason old_reason
            FROM otc_conditional_plans p JOIN otc_plan_assessments a ON a.plan_id=p.id
            WHERE a.reason IN (?,?,?,?) AND p.expires_at+60<=?
        """,
                (*LEGACY, now),
            )
        ]
    if not rows:
        return dict(corrected=[], waiting=0)
    usage = await provider.usage()
    symbols = sorted({r["symbol"] for r in rows})
    if usage.remaining < len(symbols) + 10:
        raise RuntimeError("Insufficient quota for exact reassessment")
    histories = {}
    for symbol in symbols:
        histories[symbol] = await provider.candles(symbol, now)
    updates = []
    waiting = 0
    for row in rows:
        result = assess(row, histories[row["symbol"]], now)
        if result["outcome"] == "UNKNOWN":
            waiting += 1
        else:
            updates.append((row, result))
    if not updates:
        return dict(corrected=[], waiting=waiting)
    backup = db.db_path.with_name(
        db.db_path.name + f".backup-advisory-correction-{time.time_ns()}"
    )
    with db.connect() as source, closing(sqlite3.connect(backup)) as destination:
        source.backup(destination)
    changed = []
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""CREATE TABLE IF NOT EXISTS otc_assessment_corrections(
            plan_id TEXT,version TEXT,changed_at REAL,before_json TEXT,after_json TEXT,
            PRIMARY KEY(plan_id,version))""")
        for row, result in updates:
            old = connection.execute(
                "SELECT * FROM otc_plan_assessments WHERE plan_id=?", (row["id"],)
            ).fetchone()
            if old is None or old["reason"] not in LEGACY:
                continue
            inserted = connection.execute(
                "INSERT OR IGNORE INTO otc_assessment_corrections VALUES(?,?,?,?,?)",
                (
                    row["id"],
                    VERSION,
                    now,
                    json.dumps(dict(old), allow_nan=False),
                    json.dumps(result, allow_nan=False),
                ),
            ).rowcount
            if not inserted:
                continue
            connection.execute(
                """UPDATE otc_plan_assessments SET assessed_at=?,outcome=?,condition_result=?,
                entry_price=?,expiry_price=?,reason=?,evidence=? WHERE plan_id=?""",
                (
                    now,
                    result["outcome"],
                    result["condition"],
                    result.get("entry_price"),
                    result.get("expiry_price"),
                    result["reason"],
                    json.dumps(result.get("evidence", {}), allow_nan=False),
                    row["id"],
                ),
            )
            changed.append(
                dict(
                    symbol=row["symbol"],
                    entry_at=row["entry_at"],
                    before=old["outcome"],
                    after=result["outcome"],
                )
            )
    return dict(corrected=changed, waiting=waiting, backup=str(backup))
