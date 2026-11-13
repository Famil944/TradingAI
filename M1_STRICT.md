# M1 strict — experimental short-expiration filter

The user reports frequent M1 losses. This implementation does **not** assert that more filters will improve the win rate or trading P&L. It is a causal, versioned hypothesis and a paired research comparison.

## What changed

- **M1 only** now requires a consistent direction on 26 recent **fully closed** M5 bars: EMA8 vs EMA21 (with gap relative to M5 ATR), EMA8 slope, and last M5 close location. The in-progress M5 bar is not used.
- The M1 market state must be a directional trend aligned with M5. Ranging, transitional and opposite M5 conditions are skipped.
- The prior closed M1 must not be an extraordinary impulse or oversized range relative to its ATR.
- At decision time and again just before sending the Telegram card, the latest *observed* OTC tick must be no more than 3 seconds old, arrive within 2 seconds of its event time, and not indicate a sharp adverse move or an overextended advance against the already closed M1 reference price.
- M1 final quote metadata in the shared delivery guard has a 3-second maximum age (M5/M15 remain at 8 seconds). Telegram signal window remains 15–20 seconds before the chosen M1 candle start.
- **M5 and M15 strategy logic is unchanged.** The strict M1 policy has no effect on their feature calculations, direction or filters.
- All candidate snapshots retain `m1_baseline_eligible` (the old selection gate) and `m1_strict_eligible` (the extra M1 gate), including rejection reasons, under `primary-ready-v8-m1-strict-shadow`.
- The `/otc_m1 7d` command compares **matched opportunities**, showing baseline, accepted strict subset, rejected subset and difference in virtual returns **per original opportunity** (rejected entry counts as zero profit/loss).
- A prospective, canonical OTCharts historical open is required to resolve a virtual outcome. Missing or revised prices remain `UNKNOWN`. Payout is the candidate snapshot, **not a verified broker payout**.
- No model retraining, promotion or profitability optimisation is automated. No account connection or Pocket Option order execution is added.

## Testing

- `/otc_m1 24h`, `/otc_m1 7d` or `/otc_m1 30d` — compare on new candidate data. Older strategies cannot be retroactively recomputed without original observation timestamps and ticks.
- `/otc_filters 7d` and `/otc_compare 7d` — existing general source reports.
- To disable only the M1 publication filter while **continuing to log its shadow decisions**, use `OTC_M1_STRICT_ENABLED=false` in your local `.env` and restart the bot. Default is `true`.
- If the M1 feed has fewer than 160 continuous closed M1 candles, an insufficient higher-timeframe history, or no timely tick, strict M1 deliberately **issues no signal**. This is not a system error.
- Real Pocket Option price reconciliation remains the user's separate responsibility. Virtual OTCharts results must not be interpreted as proof of real trade win rates.

**Statistical caution:** compare at least several independent trading days on future, untouched data. A higher win percentage with a small, cherry-picked subset may still have lower total return. Compare virtual returns per opportunity, missing rates, and instrument breakdown together. Binary options are high-risk and can lose the full stake.
