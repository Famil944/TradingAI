# Groq OTC experiment

Groq receives numeric snapshots from the existing OTCharts stream: up to five eligible instruments, four closed M1 candles and causal EMA/RSI/MACD/ADX/ATR/Bollinger features on M1/M5/M15. It has no direct Pocket Option access. Current incomplete candles are excluded. No additional SSE connection is opened.

An independent task requests a structured UP/DOWN/SKIP decision 30–35 seconds before the next minute. Supported durations are 1/2/3/5/15 minutes. Signals are sent only 15–20 seconds before entry, after checking fresh quotes, current open status and payout >=90%. A late answer or failed validation produces no signal. Telegram acknowledgement time does not prove the arrival time on a phone.

This is an unvalidated experiment. No model confidence or invented win probability is displayed. Groq recommendations do not alter the main strategy or XGBoost. One Groq position can be pending at a time; historical OTCharts entry/expiry minute opens determine its virtual outcome. Missing history becomes UNKNOWN, not a fabricated win. No automatic trades.

`data/groq-preview.db` contains a separate signal journal, request evidence, and persistent budget/cooldown state. Telegram `/otc` → `Groq · статистика теста` offers separate duration filters. Model SKIP decisions are stored in `groq_calls`, rather than counted as issued signals.

Configuration in local `.env`:

```
GROQ_API_KEY=...
GROQ_MODEL=openai/gpt-oss-20b
GROQ_INTERVAL_SECONDS=300
GROQ_MAX_REQUESTS=100
GROQ_MAX_TOKENS=150000
```

Budgets cover a rolling 24-hour period. Requests are reserved before sending, including unsuccessful attempts; unknown token usage is conservatively charged. Actual reported tokens replace estimates. Budget exhaustion, missing key, 429, authentication errors, network timeouts and invalid output pause/skip only Groq. HTTP 429 obeys a bounded Retry-After; absent headers use a one-hour pause. No immediate retries and no automatic paid/model fallback. The primary strategy and XGBoost keep running.

Groq documentation: https://console.groq.com/docs/rate-limits and https://console.groq.com/docs/structured-outputs . Actual organization limits can differ; the local budget also protects against token exhaustion.
