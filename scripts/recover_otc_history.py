"""Read-only export of verifiable OTC forecast history (no private wallet data).

Usage: python -m scripts.recover_otc_history --root . --output data/exports
Local Windows/Termux journals are required for their own historical results.
"""
import argparse
import csv
import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from services.otc_public_stats import main_signals, metric_units, metrics, shadow_signals
from services.otc_strategies_live import load_live_signals

MSK = ZoneInfo("Europe/Moscow")
FIELDS = (
    "id", "origin", "strategy", "version", "seconds", "symbol", "direction",
    "created_at", "entry_at", "expires_at", "resolved_at", "outcome",
    "attempts", "provenance",
)


def day(at):
    return datetime.fromtimestamp(at, MSK).strftime("%Y-%m-%d")


def iso(at):
    return datetime.fromtimestamp(at, MSK).isoformat(timespec="seconds")


def export(root, output, since=0, now=None, with_shadow=False):
    root, output = Path(root), Path(output)
    now = time.time() if now is None else now
    rows = main_signals(root / "data" / "bollinger-cycles.db", since)
    rows += load_live_signals(root / "data" / "community-live.db", since)
    if with_shadow:
        rows += shadow_signals(root / "data" / "community-cycles.db", since, now)
    rows = [row for row in rows if row["created_at"] <= now]
    output.mkdir(parents=True, exist_ok=True)

    with (output / "signals.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (r["created_at"], r["id"])):
            writer.writerow({
                **row,
                "created_at": iso(row["created_at"]),
                "entry_at": iso(row["entry_at"]),
                "expires_at": iso(row["expires_at"]),
                "resolved_at": iso(row["resolved_at"]),
                "attempts": json.dumps(row.get("attempts", []), ensure_ascii=False),
            })

    headings = (
        "day", "origin", "strategy", "version", "seconds", "metric",
        "total", "known", "win", "loss", "draw", "unknown", "pending",
        "incomplete", "win_rate_percent", "coverage_percent",
    )
    daily = defaultdict(list)
    for metric in ("A", "B", "C"):
        for item in metric_units(rows, metric, now):
            key = (
                day(item["created_at"]), item["origin"], item["strategy"],
                item["version"], item["seconds"], metric,
            )
            daily[key].append(item)
    with (output / "daily.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headings)
        for key, events in sorted(daily.items()):
            stat = metrics(events)
            coverage = stat["known"] / stat["total"] * 100 if stat["total"] else 0
            writer.writerow([
                *key, stat["total"], stat["known"], stat["win"], stat["loss"],
                stat["draw"], stat["unknown"], stat["pending"], stat["incomplete"],
                f"{stat['wr']:.2f}" if stat["wr"] is not None else "",
                f"{coverage:.2f}",
            ])
    return dict(
        signals=len(rows), confirmed=sum(r["outcome"] in ("WIN", "LOSS") for r in rows),
        incomplete=sum(r["outcome"] in ("UNKNOWN", "PENDING") for r in rows),
        days=len({day(r["created_at"]) for r in rows}),
        files=[str(output / "signals.csv"), str(output / "daily.csv")],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="data/exports")
    parser.add_argument("--since", type=float, default=0)
    parser.add_argument("--include-research-archive", action="store_true",
                        help="Explicitly include older unpublished research separately.")
    parser.add_argument("--without-shadow", action="store_true",
                        help="Deprecated compatibility flag; research is excluded by default.")
    args = parser.parse_args()
    result = export(
        args.root, args.output, args.since,
        with_shadow=args.include_research_archive and not args.without_shadow
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
