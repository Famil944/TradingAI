# Prospective candidate research

The ready primary channel now records every monitored asset considered in its
15–20-second planning window, separately for M1/M5/M15. The scheduler previously
inherited the ML 20–23-second inference window, which could consume an attempt
before the ready selector's valid window. Ready now has its own matching window.

The extra v5 filters and score retain their original numerical settings. The
research layer does not activate models, change thresholds, place orders or make
additional provider requests. It starts after restarting the bot in ready mode.
Existing journals and historical outcomes are preserved.

## Evidence

`data/ready-candidates.db` contains append-only decision snapshots, keyed by
strategy version, symbol, entry and expiry. Repeated attempts cannot rewrite an
earlier decision. Snapshots contain the original indicator evidence, five separate
gate decisions, ranking score, rejection reason, selection flag and operational
block (pending position or M1 spacing). `selected` means top-ranked, not delivered.
Stale assets and absent contexts are counted, but do not get invented directions.
Nonfinite diagnostic values are stored as null.

Available live ticks also record signed movement since the last closed analysis
candle, normalized by that timeframe's ATR, with event and receipt timestamps.
Only ticks already received at the decision are eligible. The report compares a
fixed **shadow-only** hypothesis: retain full-filter candidates whose signed move
is at least -0.25 ATR. Missing live evidence does not pass that hypothesis. Actual
last-candle close time and its age at planned entry are recorded separately from
the decision's minute cutoff.

Accepted stream ticks feed a small in-memory sampler. For each scored candidate,
it requests entry +0/+1/+3/+5 seconds and the original expiry. A sample is the
first observed tick on or after the target with both its event time and receipt
time no more than two seconds late. No candles are interpolated into tick prices.
One sample may serve overlapping tolerance windows. This measures provider tick
sensitivity, not broker execution or phone delivery. Pending samples are flushed
every five seconds and on graceful shutdown. Abrupt termination may lose buffered
samples; after restart past targets are not reconstructed. Missing samples remain
unknown. No new stream subscription is opened for instruments that leave the
monitored universe.

Canonical historical opens are evaluated separately from observed ticks. Only
`otcharts_history` bars with zero revisions qualify. Canonical confirmation now
updates provenance even when its OHLC equals the provisional candle; subsequent
provisional ticks cannot overwrite canonical history.

## Reports

Telegram: `/otc_research [24h|7d|30d|all]` (default 7d), owner-only.

From the project directory:

```powershell
.\.venv\Scripts\python -m services.otc_candidate_report --output data/candidate-quality.json
```

Optional `--since` and `--until` accept UTC epoch seconds; the latter limits
available evidence as well as decisions. `--journal` and `--archive` select
local databases. Reports open databases read-only and make no API requests. When
no prospective journal exists they return `awaiting_prospective_data` without
creating one. Existing signals are not retrospectively labelled as candidates.

The full JSON reports, separately per version and expiry:

- Existing context, full extra gates, each extra gate alone, and removal of each
  extra gate; the underlying context itself already contains indicator rules.
- All qualifying candidates versus the original score's top candidate per minute.
- Counts, coverage, WIN/LOSS/DRAW/UNKNOWN/PENDING, win rate, Wilson interval and
  mean virtual return using the recorded decision payout. Unknown payout is not
  filled in. These opportunities ignore cooldown/pending constraints, so their
  returns are not an executable portfolio backtest.
- Paired differences versus full filters per scored opportunity (abstention = 0),
  with whole-Moscow-day bootstrap intervals only after seven observed days.
- Latency results for all candidates and a common complete cohort across every
  delay. Expiry stays fixed: delaying entry shortens the remaining duration.
- Full-filter results by symbol, weak/strong ADX trend context and Moscow hour.
  This does not test an independent sideways-market strategy.

## Interpretation and next experiment

The status remains `exploratory_only`; no row of this report proves improvement.
Wilson intervals do not account for correlated signals. Day bootstrap is also
exploratory and does not correct for choosing among many filters. Compare UNKNOWN
and coverage as well as win rate. The same data must not both choose a new rule
and certify it. Choose a hypothesis using an earlier period, freeze its settings,
then validate prospectively on a later untouched period with the existing ML
walk-forward/calibration gates where applicable. Insufficient verified training
data is not repaired by inventing broker verification or payouts.

No ML model or new filter is promoted automatically. Changing live thresholds or
claiming a higher win rate requires subsequent evidence; this change provides the
measurements needed to make that decision.
