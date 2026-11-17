"""Export local evidence for manual platform comparison; never alter outcomes."""

import argparse
import csv
import math
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

FIELDS = (
    "source",
    "signal_id",
    "symbol",
    "entry_at_utc_seconds",
    "expiry_at_utc_seconds",
    "direction",
    "otcharts_outcome",
    "otcharts_entry",
    "otcharts_expiry",
    "platform_entry",
    "platform_expiry",
    "platform_outcome",
    "note",
)


def export(root, destination):
    rows = []
    for source, filename in (
        ("Primary", "primary-ready.db"),
        ("Groq", "groq-preview.db"),
        ("XGBoost", "ml-preview.db"),
    ):
        path = Path(root) / filename
        if not path.exists():
            continue
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            rows.extend(
                (source, *r, "", "", "", "")
                for r in db.execute(
                    "SELECT id,symbol,entry_at,expires_at,direction,outcome,entry_price,expiry_price "
                    "FROM predictions WHERE sent_at IS NOT NULL "
                    "AND outcome NOT IN ('LATE','CANCELLED','NOT_DELIVERED') ORDER BY entry_at,id"
                )
            )
    # Refuse to overwrite a file that may already contain manual evidence.
    with Path(destination).open("x", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(FIELDS)
        writer.writerows(rows)
    return len(rows)


def compare(path):
    counts = Counter()
    seen = set()
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            key = (row["source"], row["signal_id"])
            if key in seen:
                raise ValueError("Duplicate signal in comparison")
            seen.add(key)
            counts["total"] += 1
            outcome = row["platform_outcome"].strip().upper()
            if not outcome:
                counts["unverified"] += 1
                continue
            if outcome not in ("WIN", "LOSS", "DRAW"):
                raise ValueError("Platform outcome must be WIN, LOSS or DRAW")
            prices = (row["platform_entry"].strip(), row["platform_expiry"].strip())
            if any(prices):
                if not all(prices):
                    raise ValueError("Both platform prices are required")
                entry, expiry = map(float, prices)
                if not all(math.isfinite(p) and p > 0 for p in (entry, expiry)):
                    raise ValueError("Invalid platform price")
                if row["direction"] not in ("UP", "DOWN"):
                    raise ValueError("Invalid direction")
                calculated = (
                    "DRAW"
                    if entry == expiry
                    else "WIN"
                    if ((expiry > entry) == (row["direction"] == "UP"))
                    else "LOSS"
                )
                if calculated != outcome:
                    counts["platform_evidence_conflict"] += 1
                    continue
            if row["otcharts_outcome"] not in ("WIN", "LOSS", "DRAW"):
                counts["otcharts_unresolved"] += 1
            elif outcome == row["otcharts_outcome"]:
                counts["matched"] += 1
            else:
                counts["mismatched"] += 1
    return dict(counts)


if __name__ == "__main__":
    from config.settings import ROOT

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "compare"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    print(
        export(ROOT / "data", args.path)
        if args.action == "export"
        else compare(args.path)
    )
