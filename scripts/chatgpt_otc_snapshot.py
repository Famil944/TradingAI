"""Read-only, closed M1 candles for independent demo research; no broker access."""
import argparse
import base64
import json
import math
import re
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "tradingai-otc-snapshot-v1"
DEST = "snapshot/latest.json"

def snapshot(db_path, now=None, count=180, limit=8):
    now = time.time() if now is None else now
    if not (60 <= count <= 500 and 1 <= limit <= 20):
        raise ValueError("Invalid snapshot size")
    cutoff = int(now // 60) * 60 - 60
    result = {"schema": SCHEMA, "generated_at": now, "venue": "otc",
              "provider": "otcharts", "period_seconds": 60,
              "closed_only": True, "demo_research_only": True, "assets": []}
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=5)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        symbols = db.execute(
            "SELECT symbol,MAX(time) AS latest FROM bars WHERE time<=? AND time>=? "
            "GROUP BY symbol ORDER BY latest DESC,symbol LIMIT ?",
            (cutoff, cutoff-120, limit)).fetchall()
        for symbol, _ in symbols:
            if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9_./ -]{1,50}", symbol):
                continue
            rows = db.execute(
                "SELECT time,open,high,low,close,last_observed,revisions,source "
                "FROM bars WHERE symbol=? AND time<=? ORDER BY time DESC LIMIT ?",
                (symbol, cutoff, count)).fetchall()
            bars = []
            for t,o,h,l,c,observed,revisions,source in reversed(rows):
                if (type(t) is not int or t % 60 or not all(
                    isinstance(v,(int,float)) and math.isfinite(v)
                    for v in (o,h,l,c,observed)) or
                    not (l <= min(o,c) <= max(o,c) <= h) or observed > now + 2 or
                    observed < t+60 or source not in ("otcharts_history","otcharts_ticks") or
                    type(revisions) is not int or revisions < 0):
                    continue
                bars.append([t,o,h,l,c,observed,revisions,source])
            if not bars:
                continue
            age = now - (bars[-1][0]+60)
            contiguous = 1
            for i in range(len(bars)-1,0,-1):
                if bars[i][0]-bars[i-1][0] != 60:
                    break
                contiguous += 1
            result["assets"].append({"symbol":symbol,"age_seconds":round(age,1),
                "contiguous_tail":contiguous,
                "usable":age <= 90 and contiguous >= 60,
                "bars":bars})
    result["columns"] = ["open_time_utc","open","high","low","close",
                         "last_observed_utc","revisions","source"]
    result["usable_assets"] = sum(a["usable"] for a in result["assets"])
    return result

def gh(method, endpoint, payload=None):
    cmd = ["gh","api","--hostname","github.com","--method",method,endpoint]
    if payload is not None:
        cmd += ["--input","-"]
    p = subprocess.run(cmd,input=json.dumps(payload) if payload is not None else None,
                       text=True,capture_output=True,timeout=30)
    if p.returncode:
        m = re.search(r"HTTP (\d{3})",p.stderr)
        raise RuntimeError("GitHub HTTP "+(m.group(1) if m else "unknown"))
    return json.loads(p.stdout)

def publish(data, repo):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",repo):
        raise ValueError("Invalid repository")
    meta = gh("GET",f"repos/{repo}")
    if meta.get("private") is not True or meta.get("visibility") != "private":
        raise ValueError("Destination must be private")
    branch = meta["default_branch"]
    endpoint = f"repos/{repo}/contents/{DEST}"
    payload = {"message":"Refresh OTC research snapshot","branch":branch,
               "content":base64.b64encode(json.dumps(data,allow_nan=False,
                   separators=(",",":")).encode()).decode()}
    try:
        payload["sha"] = gh("GET",endpoint+"?ref="+branch)["sha"]
    except RuntimeError as e:
        if str(e) != "GitHub HTTP 404":
            raise
    gh("PUT",endpoint,payload)
    return f"https://github.com/{repo}/blob/{branch}/{DEST}"

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db",type=Path,default=ROOT/"data/market-history.db")
    p.add_argument("--output",type=Path,default=ROOT/"data/chatgpt-otc-snapshot.json")
    p.add_argument("--repo")
    p.add_argument("--confirm-data-terms",action="store_true",
                   help="Confirm your provider permits this private data sharing")
    p.add_argument("--watch",action="store_true")
    p.add_argument("--interval",type=int,default=60)
    p.add_argument("--bars",type=int,default=180)
    p.add_argument("--assets",type=int,default=8)
    a=p.parse_args()
    if a.repo and not a.confirm_data_terms:
        p.error("Private upload requires --confirm-data-terms")
    if a.interval < 60:
        p.error("Minimum interval 60 seconds")
    while True:
        try:
            data=snapshot(a.db,count=a.bars,limit=a.assets)
            a.output.parent.mkdir(parents=True,exist_ok=True)
            temp=a.output.with_suffix(".tmp")
            temp.write_text(json.dumps(data,allow_nan=False),encoding="utf-8")
            temp.replace(a.output)
            print(f"Fresh usable assets: {data['usable_assets']}; {a.output}",flush=True)
            if a.repo:
                print(publish(data,a.repo),flush=True)
        except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as e:
            print("Snapshot failed: "+type(e).__name__,flush=True)
            if not a.watch:
                raise SystemExit(1) from None
        if not a.watch:
            break
        time.sleep(a.interval)

if __name__ == "__main__":
    main()
