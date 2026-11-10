"""Import independent platform observations, never broker credentials.

JSONL observations contain symbol, provider_at/platform_at (UTC), provider_price,
platform_price, evidence (a file/reference identifying the independent record).
Optional provider_ohlc/platform_ohlc dictionaries are stored with the evidence.
A comparison does not automatically authorize a live signal or outcome.
"""

import argparse
import json
import math
from pathlib import Path

from database.db import Database
from services.otc_market import utc_timestamp


def import_comparisons(db, rows):
    count = 0
    for row in rows:
        prices = [float(row[k]) for k in ("provider_price", "platform_price")]
        if not all(math.isfinite(p) and p > 0 for p in prices) or not row.get(
            "evidence"
        ):
            raise ValueError("Independent evidence and valid prices required")
        evidence = {"reference": row["evidence"]}
        for name in ("provider_ohlc", "platform_ohlc"):
            if name in row:
                ohlc = {
                    k: float(row[name][k]) for k in ("open", "high", "low", "close")
                }
                if (
                    not all(math.isfinite(v) and v > 0 for v in ohlc.values())
                    or ohlc["high"] < max(ohlc.values())
                    or ohlc["low"] > min(ohlc.values())
                ):
                    raise ValueError("Invalid comparison OHLC")
                evidence[name] = ohlc
        db.compare(
            source="otcharts",
            symbol=row["symbol"],
            provider_at=utc_timestamp(row["provider_at"]),
            platform_at=utc_timestamp(row["platform_at"]),
            provider_price=prices[0],
            platform_price=prices[1],
            evidence=json.dumps(evidence, allow_nan=False),
        )
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    db = Database(args.database)
    db.init_db()
    with args.input.open(encoding="utf-8") as f:
        count = import_comparisons(db, [json.loads(line) for line in f if line.strip()])
    print(
        "Imported comparisons:",
        count,
        "; platform match remains unconfirmed until reviewed.",
    )


if __name__ == "__main__":
    main()
