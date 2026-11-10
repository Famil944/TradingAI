"""Descriptive chronological monitoring; never a live probability or proof of edge."""

import json
from collections import defaultdict


def build_report(db, now):
    with db.connect() as connection:
        rows = connection.execute("""SELECT p.symbol,p.strategy,p.expires_at-p.entry_at seconds,
            p.created_at,a.outcome FROM otc_conditional_plans p
            LEFT JOIN otc_plan_assessments a ON p.id=a.plan_id
            ORDER BY p.created_at,p.id""").fetchall()
    groups = defaultdict(list)
    for row in rows:
        groups[(row["symbol"], row["seconds"], row["strategy"])].append(dict(row))

    def metrics(items):
        wins = sum(r["outcome"] == "WIN" for r in items)
        losses = sum(r["outcome"] == "LOSS" for r in items)
        return dict(
            total=len(items),
            wins=wins,
            losses=losses,
            win_rate=wins / (wins + losses) if wins + losses else None,
            skipped=sum(r["outcome"] == "SKIPPED" for r in items),
        )

    result = []
    for (symbol, seconds, strategy), items in groups.items():
        cut = int(len(items) * 0.7)
        # All plans at the same decision time stay on the same side.
        while (
            0 < cut < len(items)
            and items[cut - 1]["created_at"] == items[cut]["created_at"]
        ):
            cut -= 1
        result.append(
            dict(
                symbol=symbol,
                seconds=seconds,
                strategy=strategy,
                all=metrics(items),
                earlier=metrics(items[:cut]),
                subsequent=metrics(items[cut:]),
                advantage_confirmed=False,
            )
        )
    return dict(
        generated_at=now,
        source="otcharts_virtual",
        groups=result,
        note="Descriptive time split, not independent walk-forward proof. Payout at entry and platform matching are unverified.",
        ml_live_enabled=False,
    )


def write_report(db, now):
    destination = db.db_path.parent / "signal-quality.json"
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(build_report(db, now), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(destination)
