"""Bounded overnight process supervision and local health evidence, no trading."""

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.single_instance import SingleInstance

ROOT = Path(__file__).resolve().parents[1]
HEALTH = ROOT / "logs" / "runtime-health.json"


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )
    temporary.replace(path)


async def heartbeat(service):
    while True:
        now = time.time()
        await asyncio.to_thread(
            write_json,
            HEALTH,
            dict(
                at=now,
                pid=os.getpid(),
                run_id=os.environ.get("TRADING_AI_RUN_ID"),
                running=service.running(),
                phase=service.phase,
                quote_age=now - service.last_tick
                if service.last_tick is not None
                else None,
                catalogue_age=service.catalogue_age(),
                end=service.ends_at,
                error=service.last_error,
                user_stopped=getattr(service, "user_stopped", False),
            ),
        )
        await asyncio.sleep(15)


def restart_reason(health, now, started, run_id):
    if now - started < 180:
        return None
    if not health or health.get("run_id") != run_id or now - health.get("at", 0) > 120:
        return "heartbeat_stalled"
    if health.get("user_stopped"):
        return None
    catalogue_age = health.get("catalogue_age")
    if (
        health.get("running")
        and now - started >= 300
        and (catalogue_age is None or catalogue_age > 300)
    ):
        return "market_metadata_stalled"
    # Quota/auth failures require user intervention, not a restart storm.
    error = health.get("error") or ""
    transient = any(
        text in error for text in ("после повторных попыток", "сетевой тайм-аут")
    )
    if not health.get("running") and transient:
        return "transient_session_stopped"
    return None


def terminate_owned(child):
    if child.poll() is None:
        subprocess.run(
            ["taskkill", "/PID", str(child.pid), "/T", "/F"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()


def comparison_report(since, until, now):
    from services.otc_statistics import wilson

    rows = []
    with sqlite3.connect(ROOT / "data" / "bot.db") as db:
        rows.extend(
            ("Основной", int(duration) // 60, outcome, count)
            for duration, outcome, count in db.execute(
                """
            SELECT p.expires_at-p.entry_at,coalesce(a.outcome,'PENDING'),count(*)
            FROM otc_conditional_plans p LEFT JOIN otc_plan_assessments a ON a.plan_id=p.id
            WHERE p.expires_at>=? AND p.entry_at<? GROUP BY 1,2""",
                (since, until),
            )
        )
    for name, filename in [
        ("XGBoost", "ml-preview.db"),
        ("Groq", "groq-preview.db"),
        ("Основной v5", "primary-ready.db"),
    ]:
        if not (ROOT / "data" / filename).exists():
            continue
        with sqlite3.connect(ROOT / "data" / filename) as db:
            rows.extend(
                (name, int(duration) // 60, outcome, count)
                for duration, outcome, count in db.execute(
                    """
                SELECT expires_at-entry_at,outcome,count(*) FROM predictions
                WHERE expires_at>=? AND entry_at<? GROUP BY 1,2""",
                    (since, until),
                )
            )
    groups = {}
    for source, minutes, outcome, count in rows:
        groups.setdefault((source, minutes), {})[outcome] = count
    lines = [
        "# Ночное сравнение OTC",
        "",
        f"Период: {datetime.fromtimestamp(since, ZoneInfo('Europe/Moscow')):%d.%m %H:%M} — {datetime.fromtimestamp(until, ZoneInfo('Europe/Moscow')):%d.%m %H:%M} МСК.",
        "Виртуальные результаты по OTCharts. В статистику WIN/LOSS входят только отправленные сигналы с выполненными условиями. Отмена, пропуск, неизвестно и ожидание в винрейт не входят.",
        "",
        "| Источник | Минуты | WIN | LOSS | Пропуск/отмена | Неизвестно | Ожидают | Винрейт |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    totals = {}
    for (source, minutes), counts in sorted(groups.items()):
        wins, losses = counts.get("WIN", 0), counts.get("LOSS", 0)
        skip = sum(
            counts.get(k, 0) for k in ("SKIPPED", "CANCELLED", "NOT_DELIVERED", "LATE")
        )
        rate = f"{100 * wins / (wins + losses):.1f}%" if wins + losses else "—"
        lines.append(
            f"| {source} | {minutes} | {wins} | {losses} | {skip} | {counts.get('UNKNOWN', 0)} | {counts.get('PENDING', 0)} | {rate} |"
        )
        previous_wins, previous_losses = totals.get(source, (0, 0))
        totals[source] = (previous_wins + wins, previous_losses + losses)
    lines += [
        "",
        "Суммарные показатели разных длительностей не доказывают превосходство: сравнивайте строки одной экспирации. Модели дают разное количество сигналов.",
    ]
    for source, (wins, losses) in sorted(totals.items()):
        n = wins + losses
        if n:
            bounds = wilson(wins, losses)
            lines.append(
                f"- {source}: {wins}/{n} WIN; 95% интервал {bounds[0] * 100:.1f}–{bounds[1] * 100:.1f}%."
            )
    lines += [
        "",
        "Статистическое преимущество автоматически не объявляется. Источник цен не подтверждает исполнение реальной сделки на платформе.",
        f"Обновлено: {now:.0f} UTC epoch. Завершение сеанса: {until:.0f}.",
    ]
    (ROOT / "logs" / "MORNING_COMPARISON.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def supervise(until, since=None):
    log = ROOT / "logs" / "overnight-supervisor.jsonl"
    restarts = []
    child = None
    files = []
    run_id = None
    started = 0
    since = time.time() if since is None else since
    last_report = 0
    try:
        while time.time() < until + 240:
            now = time.time()
            if now - last_report >= 60:
                try:
                    comparison_report(since, until, now)
                except Exception as exc:
                    with log.open("a", encoding="utf-8") as output:
                        output.write(
                            json.dumps(
                                dict(
                                    at=now,
                                    event="report_error",
                                    error=type(exc).__name__,
                                )
                            )
                            + "\n"
                        )
                last_report = now
            try:
                health = json.loads(HEALTH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                health = None
            if health and health.get("run_id") == run_id and health.get("user_stopped"):
                break
            reason = None
            if now < until:
                if child is None or child.poll() is not None:
                    reason = "start" if child is None else "process_exited"
                else:
                    reason = restart_reason(health, now, started, run_id)
            if reason:
                restarts = [stamp for stamp in restarts if now - stamp < 3600]
                if len(restarts) >= 3:
                    time.sleep(30)
                    continue
                if child is not None:
                    terminate_owned(child)
                for handle in files:
                    handle.close()
                run_id = uuid.uuid4().hex
                prefix = ROOT / "logs" / ("bot-night-" + run_id[:8])
                files = [
                    Path(str(prefix) + suffix).open("a", encoding="utf-8")
                    for suffix in (".stdout.log", ".stderr.log")
                ]
                environment = dict(os.environ, PYTHONUTF8="1", TRADING_AI_RUN_ID=run_id)
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "main.py",
                        "--otc-session-minutes",
                        "480",
                        "--otc-session-end",
                        str(until),
                    ],
                    cwd=ROOT,
                    env=environment,
                    stdout=files[0],
                    stderr=files[1],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                started = now
                restarts.append(now)
                (ROOT / "logs" / "bot.pid").write_text(str(child.pid))
                with log.open("a", encoding="utf-8") as output:
                    output.write(
                        json.dumps(
                            dict(
                                at=now,
                                event=reason,
                                pid=child.pid,
                                log=str(prefix),
                                until=until,
                            )
                        )
                        + "\n"
                    )
            time.sleep(15)
    finally:
        # Keep Telegram and post-expiry evaluation available after the trading session.
        for handle in files:
            handle.close()
        with log.open("a", encoding="utf-8") as output:
            output.write(
                json.dumps(dict(at=time.time(), event="supervision_finished")) + "\n"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--until", type=float, required=True)
    parser.add_argument("--since", type=float, default=None)
    args = parser.parse_args()
    if not time.time() < args.until <= time.time() + 86400:
        parser.error("until must be within the next 24 hours")
    instance = SingleInstance(port=47657)
    if not instance.acquire():
        raise SystemExit("Supervisor already running")
    try:
        supervise(args.until, args.since)
    finally:
        instance.close()
