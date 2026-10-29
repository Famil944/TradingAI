# Pocket Option OTC scanner (experimental)

The existing Binance Spot modules are not modified. The OTC bot **does not
open trades**, connect to Pocket Option accounts, or promise accurate signals.

## Setup

1. Update the project and install the dependencies:
   pip install -r requirements.txt
2. Use the existing Telegram token (TELEGRAM_BOT_TOKEN) in local .env.
3. Add OTCHARTS_API_KEY to local .env or deployment environment. Never commit it.
4. Send /otc_id to your Telegram bot. Add OTC_ADMIN_CHAT_ID=<your chat ID>
   to local .env or deployment environment. This restricts the OTC menu to you.
5. Restart the existing bot with python main.py.
6. Send /otc or /otc_status. Choose M1/M5/M15/all, then session length.
   Buttons: 30 minutes, 1/2/4/8 hours, until stopped, stop.

## Data limitations

The OTCharts **Free** data plan has no streams and no payout percentages.
It can report plan status but cannot send actual live 90%+ signals.
A paid streaming plan is necessary to test real-time monitoring.

The scanner reads /v1/usage through the official OTCharts SDK, discovers
open OTC symbols with payout >=90% via /v1/symbols, then loads historical
1-minute bars once per symbol to warm up the indicators. It uses one live
stream for live data; it does not repeatedly poll historical candles.

Small plans cannot monitor all 90%+ assets simultaneously. The bot clearly
reports how many assets it actually streams, and it never substitutes
regular FX data or a payout guessed from a public website. The initial
watchlist is selected by payout and symbol name; expanding multi-stream
coverage requires a larger plan and a new implementation.

Pocket Option OHLC and tick timestamps from OTCharts use the broker's
UTC+2 clock and are normalized to UTC before alignment.

The filters use closed candles, EMA20/50, RSI14, ATR14, candle body
and higher-timeframe trend. These are **untested heuristics**, not a measured
win probability. Messages are *virtual signal suggestions*, not trade
results. No win/loss statistics are implemented in this stage.

## Safety

Do not copy credentials to GitHub or Telegram. Keep .env in .gitignore.
A stream-based signal can arrive after the boundary and after Telegram
delivery latency. Always check actual payout and price in Pocket Option.
Testing on a demo account is recommended before risking money.
