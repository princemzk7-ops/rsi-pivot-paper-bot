# RSI Pivot Break System — Binance Futures Paper Bot

## What it does
- Scans eligible Binance USDⓈ-M USDT perpetual pairs using public market-data endpoints.
- Uses closed 5-minute candles only.
- Recreates the core RSI-filtered pivot-line break signals from the supplied Pine Script.
- Simulates LONG/SHORT entries, ATR × 1.5 stop-loss, and TP1 at 1R.
- Simulates 0.05% taker fees per side and 0.02% slippage per side.
- Uses risk-based sizing, a maximum of 3 simultaneous paper positions, a 4-bar symbol cooldown,
  a 3% daily-loss halt, and a 15% drawdown halt.
- Writes logs to `paper_logs/`.

## Safety
This is paper trading only. It contains no API keys and does not call order-placement, cancel,
leverage, or margin-change endpoints. Never add exchange secrets to this script or chat.

## Run
Requires Python 3.10+ and internet access to Binance public Futures market-data endpoints.
No third-party packages are required.

```bash
python btcusdt_pivot_paper_bot.py --once
python btcusdt_pivot_paper_bot.py
```

For a small first test:
```bash
python btcusdt_pivot_paper_bot.py --max-symbols 30 --once
```

The `--once` option is a data/signal scan and exits; it seeds candle timestamps so it does not
open a trade based on a stale signal at startup. For continuous paper trading, start without
`--once`. On first launch, it also seeds timestamps and waits for fresh closed-candle signals.

## Important limitations
- This is an independent Python approximation of Pine Script, not a verified 1:1 TradingView
  engine. RSI/ATR initialization, ties in pivot highs/lows, exchange data timing, and Pine's
  line-array behavior can cause differences.
- The supplied Pine visuals draw TP1/TP2/TP3 but its internal `trade_dir` closure waits for the
  furthest enabled target (TP3 by default). This bot intentionally exits at TP1 as requested.
- A 5-minute candle can touch both SL and TP1; the simulator conservatively records SL first.
- If a scan misses multiple closed candles, the current version evaluates the latest closed
  candle rather than replaying every missed candle. Keep it running continuously for better
  simulation fidelity.
- Historical profitability is not established. Backtest and paper-trade for several weeks,
  include fees/slippage, and inspect `trades.csv` before considering any other mode.
- Starting equity is a simulated 100 USDT and is configurable near the top of the script.
