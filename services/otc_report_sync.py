"""Allowlisted public diagnostics, uploaded to a separate Git branch using gh auth."""

import argparse
import base64
import json
import re
import sqlite3
import subprocess
import time
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTCOMES = {
    "WIN",
    "LOSS",
    "DRAW",
    "UNKNOWN",
    "PENDING",
    "CANCELLED",
    "LATE",
    "NOT_DELIVERED",
}


def read_db(path):
    return closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    )


def export(root=ROOT, device="termux", now=None):
    """Never export env, full evidence, IDs, symbols, credentials or raw errors/logs."""
    now = time.time() if now is None else now
    root = Path(root)
    data = dict(
        schema="tradingai-public-report-v1",
        device=device,
        generated_at=now,
        generated_utc=datetime.fromtimestamp(now, UTC).isoformat(),
        virtual_only=True,
        sources={},
        candidates={},
        runtime={},
    )
    health_path = root / "logs/runtime-health.json"
    if health_path.exists():
        health = json.loads(health_path.read_text(encoding="utf-8"))
        age = max(0, now - float(health["at"]))
        data["runtime"] = dict(
            heartbeat_age_seconds=round(age, 1),
            stale=age > 120,
            running=bool(health.get("running")) and age <= 120,
            quote_age_seconds=health.get("quote_age"),
            error_present=bool(health.get("error")),
            user_stopped=bool(health.get("user_stopped")),
        )
    for source, filename in (
        ("primary", "primary-ready.db"),
        ("groq", "groq-preview.db"),
        ("xgboost", "ml-preview.db"),
    ):
        path = root / "data" / filename
        if not path.exists():
            data["sources"][source] = {"status": "no_local_journal"}
            continue
        with read_db(path) as db:
            rows = db.execute(
                "SELECT entry_at,expires_at,sent_at,outcome,model_sha,evidence "
                "FROM predictions WHERE created_at<=?",
                (now,),
            ).fetchall()
        groups = {}
        late = overdue = 0
        for entry, expiry, sent, outcome, version, evidence in rows:
            if outcome not in OUTCOMES:
                outcome = "UNKNOWN"
            # Bound and constrain version identifiers; never emit arbitrary error text.
            version = (
                version
                if isinstance(version, str)
                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", version)
                else "unknown"
            )
            for period in ("all", "24h"):
                if period == "24h" and entry < now - 86400:
                    continue
                key = (period, version, int(expiry - entry))
                groups.setdefault(key, Counter())[outcome] += 1
            late += int(sent is not None and not 15 <= entry - sent <= 20)
            overdue += int(outcome == "PENDING" and expiry + 240 < now)
        summaries = []
        for (period, version, seconds), counts in sorted(groups.items()):
            w, loss = counts["WIN"], counts["LOSS"]
            summaries.append(
                dict(
                    period=period,
                    version=version,
                    seconds=seconds,
                    outcomes=dict(counts),
                    resolved=w + loss,
                    win_rate=w / (w + loss) if w + loss else None,
                )
            )
        data["sources"][source] = dict(
            groups=summaries,
            late_deliveries=late,
            overdue_pending=overdue,
            latest_sent_at=max((r[2] for r in rows if r[2] is not None), default=None),
        )
    path = root / "data/ready-candidates.db"
    if path.exists():
        with read_db(path) as db:
            rows = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM candidates WHERE observed_at>=? AND observed_at<=?",
                    (now - 86400, now),
                )
            ]
        reasons = Counter()
        confirmations = Counter()
        regimes = Counter()
        allowed = {
            "eligible",
            "filter_rejected",
            "no_context_or_insufficient_history",
            "asset_or_quote_ineligible",
            "insufficient_indicator_history",
            "no_directional_confirmation",
            "invalid_atr",
            "invalid_features",
        }
        for row in rows:
            reason = row.get("reason")
            reasons[reason if reason in allowed else "other"] += 1
            n = row.get("confirmation_count")
            if type(n) is int and 0 <= n <= 5:
                confirmations[str(n)] += 1
            regime = row.get("regime")
            if regime in (
                "range",
                "transition",
                "uptrend",
                "downtrend",
                "strong_trend",
                "weak_trend",
            ):
                regimes[regime] += 1
        data["candidates"] = dict(
            period="24h",
            count=len(rows),
            reasons=dict(reasons),
            confirmations=dict(confirmations),
            regimes=dict(regimes),
        )
    return data


class GitHubError(RuntimeError):
    def __init__(self, code=None):
        self.code = code
        super().__init__(f"GitHub request failed (HTTP {code or 'unknown'})")


def api(method, endpoint, payload=None):
    command = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
    if payload is not None:
        command += ["--input", "-"]
    process = subprocess.run(
        command,
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
    )
    if process.returncode:
        match = re.search(r"HTTP (\d{3})", process.stderr)
        raise GitHubError(int(match[1]) if match else None)
    return json.loads(process.stdout) if process.stdout.strip() else {}


def publish(data, repo, device, *, allow_public=False, branch="reports"):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Expected owner/repo")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", device):
        raise ValueError("Invalid device name")
    metadata = api("GET", f"repos/{repo}")
    if not metadata["private"] and not allow_public:
        raise ValueError("Public upload requires --allow-public")
    if branch == metadata["default_branch"]:
        raise ValueError("Reports must use a separate branch")
    try:
        api("GET", f"repos/{repo}/git/ref/heads/{branch}")
    except GitHubError as exc:
        if exc.code != 404:
            raise
        base = api("GET", f"repos/{repo}/git/ref/heads/{metadata['default_branch']}")
        api(
            "POST",
            f"repos/{repo}/git/refs",
            dict(ref=f"refs/heads/{branch}", sha=base["object"]["sha"]),
        )
    path = f"reports/{device}/latest.json"
    endpoint = f"repos/{repo}/contents/{path}"
    content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    for attempt in range(2):
        sha = None
        try:
            sha = api("GET", endpoint + f"?ref={branch}")["sha"]
        except GitHubError as exc:
            if exc.code != 404:
                raise
        payload = dict(
            message=f"Update {device} diagnostic report",
            branch=branch,
            content=base64.b64encode(content.encode()).decode(),
        )
        if sha:
            payload["sha"] = sha
        try:
            api("PUT", endpoint, payload)
            return f"https://github.com/{repo}/blob/{branch}/{path}"
        except GitHubError as exc:
            if exc.code != 409 or attempt:
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="Famil944/TradingAI")
    parser.add_argument("--device", default="termux")
    parser.add_argument("--allow-public", action="store_true")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=900)
    args = parser.parse_args()
    if args.interval < 300:
        parser.error("Minimum interval: 300 seconds")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", args.device):
        parser.error("Invalid device name")
    destination = ROOT / "data" / f"public-report-{args.device}.json"
    while True:
        try:
            data = export(device=args.device)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False),
                encoding="utf-8",
            )
            temporary.replace(destination)
            print(
                publish(data, args.repo, args.device, allow_public=args.allow_public)
                if args.upload
                else str(destination),
                flush=True,
            )
        except (
            OSError,
            ValueError,
            RuntimeError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ) as exc:
            # Do not print external stderr, environment or payloads.
            print(f"Report sync failed: {type(exc).__name__}", flush=True)
            if not args.watch:
                raise SystemExit(1) from None
        if not args.watch:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
