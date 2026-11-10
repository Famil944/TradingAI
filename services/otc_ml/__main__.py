"""Run offline research; no API keys, registrations or automatic trades."""

import argparse
from pathlib import Path

from .research import run_research, watch
from .validation import ResearchConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, default=Path("data/otc_verified_history.jsonl")
    )
    parser.add_argument("--output", type=Path, default=Path("data/ml"))
    parser.add_argument(
        "--data-kind", choices=("recorded", "synthetic"), default="recorded"
    )
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--min-new-verified", type=int, default=1000)
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    config = ResearchConfig()
    try:
        if args.watch:
            watch(
                args.input,
                args.output,
                config=config,
                data_kind=args.data_kind,
                min_new_verified=args.min_new_verified,
                interval=args.interval,
            )
        else:
            report = run_research(
                args.input, args.output, config=config, data_kind=args.data_kind
            )
            print("Research status:", report["status"])
            print("Quality report:", args.output / "QUALITY_REPORT.md")
            print("Live signals and probability display: disabled")
    except KeyboardInterrupt:
        pass
    except (ValueError, ImportError) as exc:
        # No raw history rows, network keys or credentials are printed.
        print("Research stopped:", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
