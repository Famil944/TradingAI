# OTC bug fixes: XGBoost direction and outcome provenance

## XGBoost

- The live inference routine now **chooses UP/DOWN first** from the calibrated three-class distribution, then evaluates the probability and expected-return gates for that **same** direction. A dummy UP placeholder in an input request never rejects a DOWN forecast.
- Unqualified research/shadow models record threshold diagnostics but **do not suppress research signals** merely for falling below a training report's probability floor.
- A qualified/certified model continues to respect any **explicit** probability floor and EV constraint in its verified report, including another EV check if payout has changed before delivery.
- No **implicit** 0.60 cutoff is inserted by the live integration. The offline `ResearchConfig` still defaults to 0.60 for models trained under that policy; changing it requires re-evaluating/reporting the model and is not silently done by this patch.
- Model probabilities are **not** evidence of a calibrated, independently profitable strategy unless the validation gates have actually been met.

## WIN/LOSS provenance

- Preview journals now have nullable `result_source`. Migration adds the field without rewriting old results or guessing their method.
- Main live-prospective entries resolved by OTCharts tick samples are labelled `otcharts_live_tick`.
- Canonical/provider historical opening-price fallback is labelled `otcharts_history_open`.
- Session-history-only evaluations are separately labelled `otcharts_session_ohlc`; missing data has distinct labels.
- Manual/older outcomes with absent provenance are displayed as `legacy_unattributed`, **not** fabricated as historical or live outcomes.
- Recovery from UNKNOWN via exact canonical archive gets `otcharts_canonical_reconciliation` (where supported).
- `/otc_stats` and `/otc_compare` plus the read-only Mini App disclose the method; mixing historical and live outcomes should not be treated as one comparable proof of quality.

These remain **virtual OTCharts** results, not Pocket Option broker-executed or broker-verified settlements. Different data methods can return different outcomes at a 60-second horizon.

## Web packaging

The Python wheel now includes the `web` package and the Mini App's static `.html` and `.js` assets. GitHub Actions checks their presence in a built wheel.

## No change to primary signal voting

The existing default 3/5 confirmation requirement is a configurable and deliberate filter, not a calculation bug. This patch does **not** loosen it; statistical results on a new prospective period are required to justify changing it.
