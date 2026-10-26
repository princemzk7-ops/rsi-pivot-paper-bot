#!/usr/bin/env python3
"""
RSI Pivot Break System + TP/SL — Binance USDT-M Futures PAPER BOT
Public market-data only. No API keys. NO order-placement endpoints.

Requirements: Python 3.10+ (standard library only)
Run:
    python btcusdt_pivot_paper_bot.py
Optional:
    python btcusdt_pivot_paper_bot.py --once
    python btcusdt_pivot_paper_bot.py --max-symbols 50

Important:
- Paper trading only; this script cannot place live orders.
- Signals are evaluated on CLOSED 5-minute candles.
- It approximates the supplied Pine Script's pivot/RSI logic.
- TP1 is the paper exit target (RR=1 by default), unlike the Pine script's
  visual trade_dir closure, which waits for the furthest enabled TP (TP3 by default).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import logging
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_URL = "https://fapi.binance.com"
INTERVAL = "5m"
KLINE_LIMIT = 160
POLL_SECONDS = 60
MAX_WORKERS = 8

# Match the Pine defaults shown by the user.
PIVOT_LENGTH = 2
RSI_LENGTH = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
USE_RSI_SIGNAL_FILTER = False
RSI_FILTER_OVERBOUGHT = 75
RSI_FILTER_OVERSOLD = 25
ATR_PERIOD = 14
SL_ATR_MULT = 1.5
TP1_RR = 1.0

# Conservative paper-account controls. Change these in the config section only.
STARTING_EQUITY = 100.0       # simulated USDT; not read from any account
RISK_FRACTION = 0.005         # 0.5% of current simulated equity per trade
MAX_OPEN_POSITIONS = 3
MAX_DAILY_LOSS_FRACTION = 0.03
MAX_DRAWDOWN_HALT_FRACTION = 0.15
COOLDOWN_BARS = 4
TAKER_FEE_PER_SIDE = 0.0005   # 0.05% per side
SLIPPAGE_PER_SIDE = 0.0002    # 0.02% per side
MAX_MIN_LOT_RISK_FRACTION = 0.02
MAX_SYMBOLS_DEFAULT = 0       # 0 = all eligible USDT-M perpetual pairs

DATA_DIR = Path("paper_logs")
STATE_FILE = DATA_DIR / "state.json"
TRADES_FILE = DATA_DIR / "trades.csv"
EVENTS_FILE = DATA_DIR / "events.jsonl"
LOG_FILE = DATA_DIR / "bot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("rsi-pivot-paper")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def api_get(path: str, params: dict[str, Any] | None = None, retries: int = 3) -> Any:
    query = urllib.parse.urlencode(params or {})
    url = BASE_URL + path + (("?" + query) if query else "")
    last_error = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "RSIPivotPaperBot/1.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(1.0 + attempt * 1.5)
    raise RuntimeError(f"Public API request failed for {path}: {last_error}")


def log_event(event: str, **fields: Any) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    payload = {"time": utc_now(), "event": event, **fields}
    with EVENTS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")


def load_state() -> dict[str, Any]:
    DATA_DIR.mkdir(exist_ok=True)
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state.setdefault("equity", STARTING_EQUITY)
            state.setdefault("positions", {})
            state.setdefault("last_seen", {})
            state.setdefault("last_entry_bar", {})
            state.setdefault("daily_start_equity", state["equity"])
            state.setdefault("day_utc", datetime.now(timezone.utc).date().isoformat())
            state.setdefault("halted", False)
            return state
        except (json.JSONDecodeError, OSError):
            log.warning("Could not read state file; starting a fresh paper state.")
    return {
        "equity": STARTING_EQUITY,
        "positions": {},
        "last_seen": {},
        "last_entry_bar": {},
        "daily_start_equity": STARTING_EQUITY,
        "day_utc": datetime.now(timezone.utc).date().isoformat(),
        "halted": False,
    }


def save_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp.replace(STATE_FILE)


def append_trade(row: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    exists = TRADES_FILE.exists()
    fields = [
        "entry_time", "exit_time", "symbol", "side", "entry_price", "exit_price",
        "quantity", "stop_loss", "take_profit_1", "gross_pnl", "fees", "net_pnl",
        "equity_after", "exit_reason", "signal_time",
    ]
    with TRADES_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def rma(values: list[float | None], length: int) -> list[float | None]:
    """Wilder's RMA, initialized with an SMA."""
    out: list[float | None] = [None] * len(values)
    if length <= 0 or len(values) < length:
        return out
    valid_start = None
    for i in range(len(values)):
        if i + 1 >= length and all(v is not None for v in values[i + 1 - length:i + 1]):
            valid_start = i
            break
    if valid_start is None:
        return out
    seed = sum(float(v) for v in values[valid_start + 1 - length:valid_start + 1]) / length
    out[valid_start] = seed
    prev = seed
    for i in range(valid_start + 1, len(values)):
        if values[i] is None:
            continue
        prev = (prev * (length - 1) + float(values[i])) / length
        out[i] = prev
    return out


def calc_rsi(closes: list[float], length: int) -> list[float | None]:
    changes: list[float | None] = [None]
    for i in range(1, len(closes)):
        changes.append(closes[i] - closes[i - 1])
    gains = [None if x is None else max(x, 0.0) for x in changes]
    losses = [None if x is None else max(-x, 0.0) for x in changes]
    avg_gain = rma(gains, length)
    avg_loss = rma(losses, length)
    out: list[float | None] = [None] * len(closes)
    for i, (g, l) in enumerate(zip(avg_gain, avg_loss)):
        if g is None or l is None:
            continue
        if l == 0:
            out[i] = 100.0 if g > 0 else 50.0
        elif g == 0:
            out[i] = 0.0
        else:
            rs = g / l
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def calc_atr(highs: list[float], lows: list[float], closes: list[float], length: int) -> list[float | None]:
    tr: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        if i == 0:
            tr[i] = highs[i] - lows[i]
        else:
            tr[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
    # Pine ta.atr uses RMA; first true range is valid.
    return rma(tr, length)


def parse_klines(raw: list[list[Any]]) -> list[dict[str, Any]]:
    return [{
        "open_time": int(k[0]),
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "volume": float(k[5]),
        "close_time": int(k[6]),
    } for k in raw]


def make_signal_series(candles: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[float | None]]:
    """
    Replays the supplied Pine logic over the candle history.
    Pivot candidate at (i - pivotLength), confirmed on candle i.
    RSI pivot gate mirrors maxRSI_pivot/minRSI_pivot using a length+1 window
    offset by pivotLength, as in the supplied script.
    """
    n = len(candles)
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    rsi = calc_rsi(closes, RSI_LENGTH)
    atr = calc_atr(highs, lows, closes, ATR_PERIOD)

    active_levels: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = [{"side": None, "atr": None} for _ in range(n)]

    L = PIVOT_LENGTH
    for i in range(n):
        # Pine ta.pivothigh(high, L, L): candidate at i-L, confirmed at i.
        p = i - L
        if p >= L and i < n:
            high_window = highs[p - L:p + L + 1]
            low_window = lows[p - L:p + L + 1]
            pivot_rsi_window = [x for x in rsi[max(0, i - 2 * L):i - L + 1] if x is not None]
            if len(high_window) == 2 * L + 1 and highs[p] == max(high_window):
                max_rsi = max(pivot_rsi_window) if pivot_rsi_window else None
                if max_rsi is not None and max_rsi >= RSI_OVERBOUGHT:
                    active_levels.append({"level": highs[p], "resistance": True, "active": True})
            if len(low_window) == 2 * L + 1 and lows[p] == min(low_window):
                min_rsi = min(pivot_rsi_window) if pivot_rsi_window else None
                if min_rsi is not None and min_rsi <= RSI_OVERSOLD:
                    active_levels.append({"level": lows[p], "resistance": False, "active": True})

        bull = False
        bear = False
        for level in active_levels:
            if not level["active"]:
                continue
            broken = closes[i] > level["level"] if level["resistance"] else closes[i] < level["level"]
            if broken:
                level["active"] = False
                allow = True
                if USE_RSI_SIGNAL_FILTER and rsi[i] is not None:
                    allow = (rsi[i] < RSI_FILTER_OVERBOUGHT) if level["resistance"] else (rsi[i] > RSI_FILTER_OVERSOLD)
                if allow:
                    if level["resistance"]:
                        bull = True
                    else:
                        bear = True
        if bull or bear:
            # Pine chooses BUY if bull and bear are both true on the same candle.
            side = "LONG" if bull else "SHORT"
            signals[i] = {"side": side, "atr": atr[i], "rsi": rsi[i]}
        else:
            signals[i] = {"side": None, "atr": atr[i], "rsi": rsi[i]}
    return signals, atr


def get_symbol_rules() -> dict[str, dict[str, float]]:
    info = api_get("/fapi/v1/exchangeInfo")
    rules: dict[str, dict[str, float]] = {}
    for item in info.get("symbols", []):
        if item.get("status") != "TRADING":
            continue
        if item.get("contractType") != "PERPETUAL" or item.get("quoteAsset") != "USDT":
            continue
        symbol = item.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        step = min_qty = 0.0
        min_notional = 5.0
        for f in item.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                step = float(f.get("stepSize", 0))
                min_qty = float(f.get("minQty", 0))
            elif f.get("filterType") in ("MIN_NOTIONAL", "NOTIONAL"):
                min_notional = float(f.get("notional", f.get("minNotional", min_notional)))
        if step > 0 and min_qty > 0:
            rules[symbol] = {"step": step, "min_qty": min_qty, "min_notional": min_notional}
    return rules


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor((value + 1e-12) / step) * step


def fetch_symbol(symbol: str) -> tuple[str, list[dict[str, Any]] | None, str | None]:
    try:
        raw = api_get("/fapi/v1/klines", {"symbol": symbol, "interval": INTERVAL, "limit": KLINE_LIMIT}, retries=2)
        candles = parse_klines(raw)
        # Drop the still-forming candle; only closed candles are used.
        now_ms = int(time.time() * 1000)
        candles = [c for c in candles if c["close_time"] < now_ms]
        if len(candles) < max(RSI_LENGTH + ATR_PERIOD + 10, 50):
            return symbol, None, "not enough candles"
        return symbol, candles, None
    except Exception as exc:
        return symbol, None, str(exc)


def paper_exit(position: dict[str, Any], candle: dict[str, Any]) -> tuple[float, str] | None:
    if candle["open_time"] <= position["entry_bar_time"]:
        return None
    if position["side"] == "LONG":
        sl_hit = candle["low"] <= position["stop_loss"]
        tp_hit = candle["high"] >= position["take_profit_1"]
    else:
        sl_hit = candle["high"] >= position["stop_loss"]
        tp_hit = candle["low"] <= position["take_profit_1"]

    # Conservative ambiguity handling: if SL and TP occur in the same candle, count SL.
    if sl_hit:
        return position["stop_loss"], "SL"
    if tp_hit:
        return position["take_profit_1"], "TP1"
    # Hard time exit after 12 hours (144 five-minute candles).
    bars_held = (candle["open_time"] - position["entry_bar_time"]) // (5 * 60 * 1000)
    if bars_held >= 144:
        return candle["close"], "TIME_EXIT"
    return None


def close_position(state: dict[str, Any], symbol: str, position: dict[str, Any],
                   raw_exit: float, reason: str, candle: dict[str, Any]) -> None:
    side = position["side"]
    qty = position["quantity"]
    # Adverse slippage on exit.
    exit_price = raw_exit * (1 - SLIPPAGE_PER_SIDE if side == "LONG" else 1 + SLIPPAGE_PER_SIDE)
    direction = 1 if side == "LONG" else -1
    gross = (exit_price - position["entry_price"]) * qty * direction
    fees = (position["entry_price"] * qty + exit_price * qty) * TAKER_FEE_PER_SIDE
    net = gross - fees
    state["equity"] = round(float(state["equity"]) + net, 8)
    row = {
        "entry_time": position["entry_time"],
        "exit_time": utc_now(),
        "symbol": symbol,
        "side": side,
        "entry_price": position["entry_price"],
        "exit_price": round(exit_price, 10),
        "quantity": qty,
        "stop_loss": position["stop_loss"],
        "take_profit_1": position["take_profit_1"],
        "gross_pnl": round(gross, 8),
        "fees": round(fees, 8),
        "net_pnl": round(net, 8),
        "equity_after": state["equity"],
        "exit_reason": reason,
        "signal_time": position["signal_time"],
    }
    append_trade(row)
    log_event("EXIT", **row)
    log.info("%s EXIT %s %s qty=%g exit=%.8g net=%+.4f equity=%.2f reason=%s",
             symbol, side, qty, exit_price, net, state["equity"], reason)
    del state["positions"][symbol]


def maybe_open_position(state: dict[str, Any], symbol: str, candle: dict[str, Any],
                        signal: dict[str, Any], rules: dict[str, float]) -> None:
    if signal.get("side") is None or signal.get("atr") is None:
        return
    if state.get("halted"):
        return
    if symbol in state["positions"]:
        return
    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        return

    current_bar = candle["open_time"]
    last_entry = state["last_entry_bar"].get(symbol)
    if last_entry is not None:
        bars_since = (current_bar - int(last_entry)) // (5 * 60 * 1000)
        if bars_since < COOLDOWN_BARS:
            return

    equity = float(state["equity"])
    if equity <= 0:
        state["halted"] = True
        log_event("HALT", reason="equity_nonpositive")
        return
    risk_dist = float(signal["atr"]) * SL_ATR_MULT
    entry_ref = candle["close"]
    if not math.isfinite(risk_dist) or risk_dist <= 0 or entry_ref <= 0:
        return

    side = signal["side"]
    # Adverse slippage on entry.
    entry_price = entry_ref * (1 + SLIPPAGE_PER_SIDE if side == "LONG" else 1 - SLIPPAGE_PER_SIDE)
    stop = entry_price - risk_dist if side == "LONG" else entry_price + risk_dist
    tp1 = entry_price + risk_dist * TP1_RR if side == "LONG" else entry_price - risk_dist * TP1_RR

    risk_budget = equity * RISK_FRACTION
    qty_by_risk = risk_budget / risk_dist
    qty_by_notional = (equity * 5.0 * 0.90) / entry_price  # simulated 5x ceiling with 10% margin buffer
    qty = min(qty_by_risk, qty_by_notional)
    qty = floor_to_step(qty, rules["step"])
    qty = max(0.0, qty)

    # If rounding up to minimum lot would exceed risk limits, skip instead.
    min_qty = max(rules["min_qty"], math.ceil(rules["min_notional"] / entry_price / rules["step"]) * rules["step"])
    if qty < min_qty:
        min_qty_risk = min_qty * risk_dist
        if min_qty_risk > equity * MAX_MIN_LOT_RISK_FRACTION:
            log_event("SKIP", symbol=symbol, reason="minimum_lot_risk_too_high",
                      min_qty=min_qty, min_qty_risk=round(min_qty_risk, 8), equity=equity)
            return
        # Paper sim permits minimum valid lot only if it fits the simulated margin cap.
        if min_qty * entry_price > equity * 5.0 * 0.90:
            log_event("SKIP", symbol=symbol, reason="minimum_notional_exceeds_simulated_margin")
            return
        qty = min_qty

    notional = qty * entry_price
    if notional < rules["min_notional"]:
        log_event("SKIP", symbol=symbol, reason="below_exchange_min_notional", notional=notional)
        return

    position = {
        "side": side,
        "entry_price": entry_price,
        "quantity": qty,
        "stop_loss": stop,
        "take_profit_1": tp1,
        "entry_time": utc_now(),
        "entry_bar_time": current_bar,
        "signal_time": datetime.fromtimestamp(current_bar / 1000, timezone.utc).isoformat(),
        "risk_budget": risk_budget,
        "entry_fee_estimate": entry_price * qty * TAKER_FEE_PER_SIDE,
    }
    state["positions"][symbol] = position
    state["last_entry_bar"][symbol] = current_bar
    log_event("ENTRY", symbol=symbol, **position, notional=notional)
    log.info("%s PAPER %s qty=%g entry=%.8g SL=%.8g TP1=%.8g notional=%.2f",
             symbol, side, qty, entry_price, stop, tp1, notional)


def main() -> int:
    parser = argparse.ArgumentParser(description="RSI Pivot Break System paper-only Binance Futures scanner")
    parser.add_argument("--once", action="store_true", help="scan once and exit")
    parser.add_argument("--max-symbols", type=int, default=MAX_SYMBOLS_DEFAULT,
                        help="limit symbols for a test; 0 means all eligible pairs")
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    log.info("PAPER MODE ONLY — public market data; no API keys; no order endpoints.")
    log.info("Timeframe=%s | TP1 RR=%.1f | simulated starting equity=%.2f USDT",
             INTERVAL, TP1_RR, STARTING_EQUITY)

    state = load_state()
    try:
        rules = get_symbol_rules()
    except Exception as exc:
        log.error("Could not load exchange symbol rules: %s", exc)
        return 2
    symbols = sorted(rules)
    if args.max_symbols and args.max_symbols > 0:
        symbols = symbols[:args.max_symbols]
    log.info("Eligible USDT-M perpetual symbols: %d", len(symbols))

    # Seed timestamps on first run so the bot does not enter a stale historical signal.
    first_run = not bool(state.get("last_seen"))
    last_scan_candle = None

    while True:
        today = datetime.now(timezone.utc).date().isoformat()
        if state.get("day_utc") != today:
            state["day_utc"] = today
            state["daily_start_equity"] = float(state["equity"])
            state["halted"] = False
            log_event("DAILY_RESET", equity=state["equity"])
        if float(state["equity"]) <= STARTING_EQUITY * (1 - MAX_DRAWDOWN_HALT_FRACTION):
            state["halted"] = True
            log_event("HALT", reason="equity_drawdown_limit", equity=state["equity"])
        daily_start = float(state.get("daily_start_equity", state["equity"]))
        if daily_start > 0 and float(state["equity"]) <= daily_start * (1 - MAX_DAILY_LOSS_FRACTION):
            state["halted"] = True
            log_event("HALT", reason="daily_loss_limit", equity=state["equity"])
        if state.get("halted"):
            log.warning("Trading halted by risk limit. Existing paper positions will still be monitored.")

        scan_started = time.time()
        results: list[tuple[str, list[dict[str, Any]] | None, str | None]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(fetch_symbol, s) for s in symbols]
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())

        good = 0
        newest_closed = None
        for symbol, candles, error in results:
            if not candles:
                if error:
                    log_event("DATA_ERROR", symbol=symbol, error=error)
                continue
            good += 1
            latest = candles[-1]
            newest_closed = max(newest_closed or latest["open_time"], latest["open_time"])

            # For open paper positions, only act once per newly closed candle.
            pos = state["positions"].get(symbol)
            if pos:
                last_checked = int(pos.get("last_checked_bar_time", pos["entry_bar_time"]))
                if latest["open_time"] > last_checked:
                    outcome = paper_exit(pos, latest)
                    pos["last_checked_bar_time"] = latest["open_time"]
                    if outcome:
                        close_position(state, symbol, pos, outcome[0], outcome[1], latest)

            signals, _ = make_signal_series(candles)
            latest_signal = signals[-1]
            previous_seen = state["last_seen"].get(symbol)
            if previous_seen is None:
                state["last_seen"][symbol] = latest["open_time"]
                continue
            if latest["open_time"] > int(previous_seen):
                state["last_seen"][symbol] = latest["open_time"]
                # If several candles elapsed between scans, only the latest closed bar is acted on.
                if not first_run and latest_signal.get("side"):
                    maybe_open_position(state, symbol, latest, latest_signal, rules[symbol])
            # If equal, no duplicate action on the same closed candle.

        first_run = False
        save_state(state)
        log.info("Scan complete: data=%d/%d symbols, open_positions=%d, equity=%.2f, halted=%s, %.1fs",
                 good, len(symbols), len(state["positions"]), state["equity"], state["halted"],
                 time.time() - scan_started)
        if args.once:
            break
        time.sleep(POLL_SECONDS)

    log.info("Stopped. Logs: %s", DATA_DIR.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
