# Primary ready signals

## Current v6: at least one directional confirmation

`primary-ready-v6-one-confirmation` replaces the v5 entry rules at the owner's
request. It accepts a direction with at least one of the five existing observed
confirmations. UP/DOWN is chosen by the larger vote count; equal counts use the
fixed EMA, MACD, candle, RSI, alignment order. Among assets, the highest count wins
with symbol as a deterministic tie breaker. Zero votes or insufficient/stale data
do not generate a forecast. Existing freshness, payout/open, delivery window,
independent pending horizons and M1 spacing checks remain.

The former context/ADX/RSI/Bollinger/stretch gates no longer block the primary
channel. The old selector remains available only for v5 regression/research.
New records carry a different version and reports do not apply v5 ablations to v6.
Signal cards show `Индикаторы: N/5` for all sources. ML/Groq still make their own
directional decisions; the new minimum-vote primary strategy is not a claim of
validated accuracy.

Cards also show a descriptive state of the analysis timeframe, based exclusively
on the same closed candles: ADX below 18 = range; ADX at least 25 and EMA spread at
least 0.1 ATR = trend in the EMA direction; otherwise transition. An opposite last
close-to-close move in a trend is labelled possible pullback; candle range at
least 2.5 ATR is a volatility spike. These fixed hypotheses are recorded as
`closed-regime-v1`, do not add votes and do not block signals.

## Archived v5 behavior

Prospective candidate/filter/latency research is described in
[CANDIDATE_RESEARCH.md](CANDIDATE_RESEARCH.md). `/otc_research` shows its compact
report; `python -m services.otc_candidate_report` generates the local report.
The ready scheduler now explicitly uses the same 15–20-second window as its selector.

User-selected replacement for primary advance conditional plans. The new primary channel publishes direction and expiry 15–20 seconds before the next minute, using only candles completed at the preceding minute boundary. There is no future candle colour condition and no later context-trigger cancellation. Payout/open/freshness checks still apply before publication. A late Telegram acknowledgement is excluded from WIN/LOSS.

M1/M5/M15 use their respective existing closed-candle context rules, with M1/higher-frame alignment. Additional fixed experimental gates require ADX >=20, aligned MACD histogram, bounded RSI/Bollinger position and distance from EMA20 <=1.5 ATR. Candidates from the current stream are ranked by recorded trend-strength/alignment score. These gates are hypotheses, not calibrated probabilities or proven higher accuracy.

Each expiry has an independent pending position. M5 and M15 wait for their previous result; M1 retains two-minute entry spacing. Outcomes use canonical historical OTCharts minute opens, including when an instrument leaves the current stream. Missing boundary data becomes UNKNOWN. No automatic trades.

The separate `data/primary-ready.db` journal stores strategy version `primary-ready-v5`, inputs, quality score, delivery and outcomes. `/otc_stats` displays v5 statistics by M1/M5/M15 when `OTC_PRIMARY_MODE=ready`. Old conditional records remain in `data/bot.db`; cycle statistics are explicitly labelled as their archive. The morning comparison lists v5 separately.

Diagnostics on the previous night's published-plan sample did **not** validate either removing preflight cancellations, enforcing two prior stable contexts, or merely reevaluating the same conditional opportunities later. They were not enabled. Their small, conditional retrospective sample cannot establish prospective performance. The v5 format is a new explicitly experimental prospective test; an improved WIN rate has not been established.

Local configuration: `OTC_PRIMARY_MODE=ready` (previous mode: `conditional`). A running session must be restarted to apply configuration changes; already entered positions remain accounted for and future old plans are cancelled.
