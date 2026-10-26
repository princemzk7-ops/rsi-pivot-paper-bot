# RSI Pivot Break Paper Bot — GitHub Actions setup

## Safety
- Paper trading only. The bot uses public Binance Futures market data.
- No API keys/secrets are required.
- No live order placement is implemented.
- GitHub Actions scheduled runs can be delayed and are not guaranteed to run exactly every 5 minutes. This is not a guaranteed 24/7 service.

## Deploy from a mobile phone
1. Sign in or create a GitHub account at https://github.com.
2. Create a **private** repository, for example `rsi-pivot-paper-bot`.
3. Upload the contents of this ZIP into the repository. Make sure the `.github/workflows/paper-bot.yml` file is included. If your phone's upload screen hides dot-folders, create the workflow file in GitHub using **Add file → Create new file** and name it `.github/workflows/paper-bot.yml`.
4. In the repository, open **Actions**. If GitHub asks, enable workflows.
5. Select **RSI Pivot Break Paper Bot** and tap **Run workflow** for the first run.
6. Later runs are scheduled every 5 minutes, subject to GitHub scheduling delays and account/repository limits.
7. Open the `paper_logs` folder in the repository to inspect `trades.csv`, `events.jsonl`, `state.json`, and `bot.log`.

## Important
- Keep the repository private because the workflow commits paper logs and state to it.
- Do not add exchange API keys or secrets; this paper bot does not need them.
- The bot scans all eligible USDT-M perpetual pairs, which may take longer than a 5-minute interval. If runs overlap, GitHub Actions concurrency queues them.
- The first run seeds candle timestamps to avoid trading stale historical signals; it may not open a paper trade immediately.
- This Python implementation approximates the supplied Pine Script; it has not been verified to match TradingView 1:1. Run it in paper mode for several weeks and review results before relying on the strategy.
