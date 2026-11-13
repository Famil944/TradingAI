# OTC reliability changes — 2026-11-13

These changes are experimental research safeguards, **not** a demonstration of profitability or a verified match between OTCharts and Pocket Option.

- Main `ready` signals now require 3/5 aligned indicators by default instead of 1/5. Set `OTC_READY_MIN_CONFIRMATIONS` to 2–5 to adjust. The higher threshold trades fewer opportunities; it has not been shown to raise win rate.
- Only one legacy/ready delivery path is permitted in a session. The `ready` profile refuses REST snapshots when a live stream is not available.
- Catalogue refresh defaults to 50 seconds, under the existing 60-second metadata recency gate, to reduce API usage (rather than every 30 seconds). Adjustable by `OTC_CATALOGUE_REFRESH_SECONDS` (15–55).
- Offline ML collectors are now opt-in: `OTC_ML_TRAINING=true`. They are resource intensive on Android. They run every five minutes by default (`OTC_ML_COLLECT_SECONDS=300`) and do not re-export entire unchanged archives. Child processes launched by the bot are reaped on clean shutdown.
- Render starts via `scripts/start_render.sh` to persist `data/` archives, model reports, journals, and signal records at `/var/data`. Existing local Termux files are untouched.
- Fresh-payout evidence and actual Pocket Option settlement prices cannot be fabricated from the existing OTCharts API; strict mode may remain unable to generate signals. Independent side-by-side quote and payout observations are required before any real-world accuracy claim.
- Offline test automation is not a live broker test. No automatic real trading is added.

## Suggested verification

Run `python -m unittest discover -s tests -v`, then compare OTCharts vs Pocket Option quotes with synchronized timestamps, watch request quotas, and analyze prospectively logged signal results. Do not rely on the virtual OTCharts-only WIN/LOSS rate as a real-platform success rate.
