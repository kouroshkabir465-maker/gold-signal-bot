# Gold signal bot
XAU/USD Ichimoku + RSI signals (4H trend, 15m entry) to Telegram, run on GitHub Actions.
- `reply.yml` — triggered by the Cloudflare Worker (worker/worker.js) on each Telegram message.
- `watcher.yml` — every 15 min, every day (signals only when market open): news alerts, trade tracking, new signals.
- `news.yml` — weekdays 04:00 UTC (07:00 Istanbul) daily high-impact USD calendar.
- `weekly.yml` — Fri 20:20 UTC (23:20 Istanbul) weekly summary.
Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TWELVEDATA_API_KEY`. State lives on the `state` branch.
Not financial advice.
