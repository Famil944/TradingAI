"""Prospective shadow comparison on the same recorded confirmed plans; no profit claims."""

import json
import math
from collections import defaultdict


def comparison(db, now):
    groups = defaultdict(list)
    with db.connect() as connection:
        rows = connection.execute(
            """
            SELECT p.expires_at-p.entry_at seconds,p.entry_at,p.expires_at,
                e.checked_at,e.evidence,a.outcome,a.assessed_at
            FROM otc_conditional_plans p JOIN otc_entry_checks e ON e.plan_id=p.id
            JOIN otc_plan_assessments a ON a.plan_id=p.id
            WHERE e.decision='MET' AND a.outcome IN ('WIN','LOSS','DRAW')
              AND p.expires_at<=? AND a.assessed_at<=?
        """,
            (now, now),
        ).fetchall()
    for row in rows:
        evidence = json.loads(row["evidence"])
        ml = evidence.get("xgboost", {})
        predicted = ml.get("predicted_at")
        if ml.get("status") != "shadow" or not isinstance(predicted, (int, float)):
            continue
        if (
            not row["entry_at"]
            <= predicted
            <= min(row["entry_at"] + 5, row["assessed_at"])
        ):
            continue
        market = evidence.get("entry_market") or {}
        payout = market.get("payout")
        net = None
        if isinstance(payout, (int, float)) and math.isfinite(payout) and 0 <= payout <= 100:
            net = {"WIN": payout / 100, "LOSS": -1.0, "DRAW": 0.0}[row["outcome"]]
        groups[(row["seconds"], ml["model_sha256"])].append(
            dict(
                outcome=row["outcome"],
                selected=bool(ml.get("accept")),
                day=int(row["entry_at"]) // 86400,
                net=net,
            )
        )

    def metrics(items):
        wins = sum(r["outcome"] == "WIN" for r in items)
        losses = sum(r["outcome"] == "LOSS" for r in items)
        returns = [r["net"] for r in items if r["net"] is not None]
        return dict(
            opportunities=len(items),
            wins=wins,
            losses=losses,
            win_rate=wins / (wins + losses) if wins + losses else None,
            draws=len(items) - wins - losses,
            known_payout_count=len(returns),
            net_units=sum(returns) if returns else None,
            mean_net_units=sum(returns) / len(returns) if returns else None,
        )

    return dict(
        generated_at=now,
        source="prospective_otcharts_virtual",
        live_enabled=False,
        groups=[
            dict(
                seconds=seconds,
                model_sha256=sha,
                baseline=metrics(items),
                ml_filter=metrics([r for r in items if r["selected"]]),
                rejected=metrics([r for r in items if not r["selected"]]),
                coverage=sum(r["selected"] for r in items) / len(items),
                days=len({r["day"] for r in items}),
                advantage_confirmed=False,
            )
            for (seconds, sha), items in groups.items()
        ],
    )
