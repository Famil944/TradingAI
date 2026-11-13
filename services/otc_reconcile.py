"""Recover UNKNOWN virtual outcomes only from exact, unrevised canonical opens."""

import argparse
import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def reconcile(journal, archive, *, since=0, apply=False, now=None):
    now = time.time() if now is None else now
    journal, archive = Path(journal), Path(archive)
    results = []
    with (
        closing(
            sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db,
        closing(
            sqlite3.connect(archive.resolve().as_uri() + "?mode=ro", uri=True)
        ) as history,
    ):
        db.row_factory = sqlite3.Row
        for row in db.execute(
            "SELECT * FROM predictions WHERE outcome='UNKNOWN' AND sent_at>=?", (since,)
        ):
            row = dict(row)
            item = dict(id=row["id"], symbol=row["symbol"], old_outcome="UNKNOWN")
            if (
                not 15 <= row["entry_at"] - row["sent_at"] <= 20
                or now < row["expires_at"] + 60
            ):
                results.append(dict(item, status="ineligible_timing"))
                continue
            prices = []
            evidence = []
            for target in (row["entry_at"], row["expires_at"]):
                bar = history.execute(
                    "SELECT open,source,revisions,last_observed FROM bars WHERE symbol=? AND time=?",
                    (row["symbol"], target),
                ).fetchone()
                if (
                    not bar
                    or bar[1] != "otcharts_history"
                    or bar[2] != 0
                    or not math.isfinite(bar[0])
                    or bar[0] <= 0
                    or bar[3] > now
                ):
                    break
                prices.append(bar[0])
                evidence.append(
                    dict(
                        time=target,
                        open=bar[0],
                        source=bar[1],
                        revisions=bar[2],
                        observed_at=bar[3],
                    )
                )
            if len(prices) != 2 or row["direction"] not in ("UP", "DOWN"):
                results.append(dict(item, status="missing_or_revised_canonical_price"))
                continue
            delta = prices[1] - prices[0]
            outcome = (
                "DRAW"
                if delta == 0
                else "WIN"
                if (delta > 0) == (row["direction"] == "UP")
                else "LOSS"
            )
            results.append(
                dict(
                    item,
                    status="recoverable",
                    outcome=outcome,
                    entry_price=prices[0],
                    expiry_price=prices[1],
                    bars=evidence,
                )
            )
    if apply and any(r["status"] == "recoverable" for r in results):
        backup = journal.with_name(journal.name + f".backup-reconcile-{time.time_ns()}")
        with (
            closing(sqlite3.connect(journal)) as db,
            closing(sqlite3.connect(backup)) as copy,
        ):
            db.backup(copy)
        with closing(sqlite3.connect(journal, timeout=5)) as db:
            db.execute("BEGIN IMMEDIATE")
            for item in results:
                if item["status"] != "recoverable":
                    continue
                old = db.execute(
                    "SELECT evidence FROM predictions WHERE id=? AND outcome='UNKNOWN'",
                    (item["id"],),
                ).fetchone()
                if not old:
                    item["status"] = "already_changed"
                    continue
                evidence = json.loads(old[0])
                evidence["reconciliation"] = dict(
                    at=now,
                    previous_outcome="UNKNOWN",
                    source="local_canonical_archive",
                    bars=item["bars"],
                )
                db.execute(
                    "UPDATE predictions SET outcome=?,entry_price=?,expiry_price=?,evidence=? WHERE id=? AND outcome='UNKNOWN'",
                    (
                        item["outcome"],
                        item["entry_price"],
                        item["expiry_price"],
                        json.dumps(evidence, allow_nan=False),
                        item["id"],
                    ),
                )
                item["status"] = "recovered"
            db.commit()
    return results


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=root / "data/primary-ready.db")
    parser.add_argument("--archive", type=Path, default=root / "data/market-history.db")
    parser.add_argument("--since", type=float, default=0)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            reconcile(args.journal, args.archive, since=args.since, apply=args.apply),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
