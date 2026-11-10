"""Virtual four-attempt cycles over subsequent delivered, non-overlapping signals."""


def summarize(rows, since=0):
    wins = losses = skipped = unavailable = 0
    stage = 0
    ready_at = float("-inf")
    unfinished = False
    for row in rows:
        # A recovery cannot use a plan already announced before the prior outcome was known.
        if row["created_at"] < ready_at:
            continue
        outcome = row["outcome"]
        if outcome in ("SKIPPED", "CANCELLED", "NOT_DELIVERED") or row["delivery"] in (
            "FAILED",
            "LATE",
        ):
            if row["created_at"] >= since:
                skipped += 1
            continue
        if outcome == "UNKNOWN":
            unavailable += 1
            stage = 0
            ready_at = max(row["expires_at"], row["assessed_at"])
            continue
        if outcome is None or row["delivery"] != "SENT":
            unavailable += 1
            unfinished = True
            break
        known_at = max(row["expires_at"], row["assessed_at"])
        ready_at = known_at
        if outcome == "DRAW":
            continue
        if outcome == "WIN":
            if known_at >= since:
                wins += 1
            stage = 0
        elif outcome == "LOSS":
            stage += 1
            if stage == 4:
                if known_at >= since:
                    losses += 1
                stage = 0
    return dict(
        wins=wins,
        losses=losses,
        skipped=skipped,
        stage=stage,
        pending=unfinished or stage > 0,
        unavailable=unavailable,
    )


def read_stats(db, seconds, since=0):
    with db.connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
            SELECT p.created_at,p.entry_at,p.expires_at,p.delivery,a.outcome,a.assessed_at
            FROM otc_conditional_plans p LEFT JOIN otc_plan_assessments a ON a.plan_id=p.id
            WHERE p.expires_at-p.entry_at=? ORDER BY p.created_at,p.entry_at,p.id
        """,
                (seconds,),
            )
        ]
    return summarize(rows, since)


def format_cycles(stats, minutes):
    wins, losses = stats["wins"], stats["losses"]
    rate = f"{100 * wins / (wins + losses):.1f}%" if wins + losses else "—"
    text = (
        f"M{minutes} · циклы ×4, виртуально\nВин {wins}\nЛос {losses}\n"
        f"Пропуск {stats['skipped']}\nВинрейт {rate}"
    )
    if stats["unavailable"]:
        text += f"\nНет данных {stats['unavailable']}"
    if stats["pending"]:
        text += (
            "\nЦикл не завершён"
            if stats["unavailable"]
            else f"\nСледующая ступень {stats['stage'] + 1}/4"
        )
    return text
