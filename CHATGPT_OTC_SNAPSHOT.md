# Private OTC snapshots for ChatGPT

This standalone exporter reads existing closed M1 bars in data/market-history.db.
It never starts a second provider connection, reads .env, places orders, or changes the bot.
Source: OTCharts, not independently verified Pocket Option execution prices.

From the TradingAI working directory on the device running the bot:
```sh
gh auth status
mkdir -p scripts
gh api 'repos/Famil944/TradingAI/contents/scripts/chatgpt_otc_snapshot.py?ref=codex/chatgpt-otc-snapshot' --jq .content | base64 -d > scripts/chatgpt_otc_snapshot.py
python scripts/chatgpt_otc_snapshot.py
```
A local JSON file is produced in data/chatgpt-otc-snapshot.json. Upload to this chat manually if desired.

Only if your OTCharts agreement permits exporting and sharing these candles privately:
```sh
gh repo create Famil944/TradingAI-market-private --private --add-readme
python scripts/chatgpt_otc_snapshot.py --repo Famil944/TradingAI-market-private --confirm-data-terms --watch --interval 60
```
Do not run the create command again if the repository already exists. The destination must be private. Grant ChatGPT GitHub access to this repository through your installed GitHub integration. Never publish the snapshots in the public TradingAI repository. GitHub retains commit history, so prefer on-demand exports where possible. Stop watch with Ctrl+C.

Check snapshot/latest.json in the private repo. Reject snapshots if the generation time or last candle is stale at analysis time; require a continuous tail of closed bars and sufficient history for the chosen timeframe. A fresh snapshot is not a live tick feed; latency makes it unsuitable for precise second-level entry or 1-minute signals. No win-rate is implied. Test on demo only. No restart of the bot is required. The exporter has not been run on the production phone from this chat.
