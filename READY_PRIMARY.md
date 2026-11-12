# Primary v5: ready-signal experiment

User-selected replacement for primary advance conditional plans. The new primary channel publishes direction and expiry 15–20 seconds before the next minute, using only candles completed at the preceding minute boundary. There is no future candle colour condition and no later context-trigger cancellation. Payout/open/freshness checks still apply before publication. A late Telegram acknowledgement is excluded from WIN/LOSS.

M1/M5/M15 use their respective existing closed-candle context rules, with M1/higher-frame alignment. Additional fixed experimental gates require ADX >=20, aligned MACD histogram, bounded RSI/Bollinger position and distance from EMA20 <=1.5 ATR. Candidates from the current stream are ranked by recorded trend-strength/alignment score. These gates are hypotheses, not calibrated probabilities or proven higher accuracy.

Each expiry has an independent pending position. M5 and M15 wait for their previous result; M1 retains two-minute entry spacing. Outcomes use canonical historical OTCharts minute opens, including when an instrument leaves the current stream. Missing boundary data becomes UNKNOWN. No automatic trades.

The separate `data/primary-ready.db` journal stores strategy version `primary-ready-v5`, inputs, quality score, delivery and outcomes. `/otc_stats` displays v5 statistics by M1/M5/M15 when `OTC_PRIMARY_MODE=ready`. Old conditional records remain in `data/bot.db`; cycle statistics are explicitly labelled as their archive. The morning comparison lists v5 separately.

Diagnostics on the previous night's published-plan sample did **not** validate either removing preflight cancellations, enforcing two prior stable contexts, or merely reevaluating the same conditional opportunities later. They were not enabled. Their small, conditional retrospective sample cannot establish prospective performance. The v5 format is a new explicitly experimental prospective test; an improved WIN rate has not been established.

Local configuration: `OTC_PRIMARY_MODE=ready` (previous mode: `conditional`). A running session must be restarted to apply configuration changes; already entered positions remain accounted for and future old plans are cancelled.
