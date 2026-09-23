#!/usr/bin/env python3
"""
Breakout + 50% Retest — 15m Live Signal Bot
=============================================

Implements the grid-search-validated configuration found in the 540-combo
optimization study (see OPTIMIZATION_REPORT.md / COMBINED_ES_NQ_REPORT.md,
not included in this bundle):

    Pivot lookback   : left=7, right=7 bars (swing high/low confirmation)
    Retest window    : 25 bars to pull back into the zone after breakout
    Retest tolerance : 0.15 of the impulse-leg range, around the 50% level
    Target           : 2.5R (rr_target — sizes the alert's displayed target
                        only; the entry signal itself doesn't depend on it)
    Invalidation     : close beyond retest_level +/- 2xtol before confirming
    Session filter   : none (all hours) — RTH filtering left too little
                        out-of-sample data to validate
    Symbols          : ES=F, NQ=F (the only two instruments this config
                        was actually backtested on)

Entry logic:
  LONG:
    1. Find swing highs/lows (pivots) on 15-min candles.
    2. Breakout: a candle CLOSES above the most recent swing high.
    3. Impulse leg = swing_low (before the swing high) -> swing_high.
    4. Retest level = swing_high - 0.5 * (swing_high - swing_low).
    5. Within `retest_window` candles, price must pull back into the
       retest zone (with tolerance) and then print a bullish confirmation
       candle (close > open, closing back above the zone).
    6. Invalidation: if price closes well past the retest zone
       (retest_level - 2*tol) before confirming, the setup is cancelled.
  SHORT is the mirror image.

*** READ BEFORE RUNNING LIVE ***
  - This sends ALERTS ONLY. It does not place trades. Wiring it to
    auto-execute is a separate, much higher-stakes step.
  - This is a "didn't fall apart out-of-sample on ~5-6 months of data"
    result, not a proven edge. Re-validate periodically as new data comes
    in — most parameter combinations that look good on one sample stop
    working out-of-sample.
  - yfinance's intraday data has a real-time lag and only ~60 days of 15m
    history; swap in a broker/exchange API (Databento, Tradovate, etc.)
    for anything production-grade.
  - No slippage or commissions are modeled in the backtest this config was
    chosen from.
  - Always dry-run first (DRY_RUN=1) to confirm alerts look right before
    pointing this at a real Telegram chat.

Environment variables required:
    TELEGRAM_TOKEN       - Telegram bot token from @BotFather
    TELEGRAM_CHAT_ID     - Telegram chat/channel ID to post signals to

Optional:
    DRY_RUN              - "1"/"true"/"yes" to log alerts instead of sending
    RETEST_STATE_FILE    - override the state file path (default: alert_state.json)

Designed to run on a polling schedule (GitHub Actions cron, every 15 min)
against fresh 15-minute candles from yfinance. State is persisted to
alert_state.json (committed back to the repo by the workflow) so a symbol
only alerts once per closed candle.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd
import requests
import yfinance as yf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("breakout-retest")

# ---------------------------------------------------------------------------
# Strategy configuration
# ---------------------------------------------------------------------------
# One entry per symbol you want the bot to watch. Each symbol carries its own
# copy of the strategy parameters so you can tune them independently later.

SYMBOLS = [
    {
        "ticker": "ES=F",
        "label": "ES",
        "multiplier": 50.0,           # $ per point, for the alert's risk/reward math
        "pivot_left": 7,
        "pivot_right": 7,
        "retest_window": 25,
        "retest_tolerance": 0.15,
        "rr_target": 2.5,
        "volume_multiplier": None,    # None disables the volume filter
        "volume_ma_window": 20,
    },
    {
        "ticker": "NQ=F",
        "label": "NQ",
        "multiplier": 20.0,
        "pivot_left": 7,
        "pivot_right": 7,
        "retest_window": 25,
        "retest_tolerance": 0.15,
        "rr_target": 2.5,
        "volume_multiplier": None,
        "volume_ma_window": 20,
    },
]

BAR_INTERVAL_MINUTES = 15
STATE_FILE = os.environ.get("RETEST_STATE_FILE", "alert_state.json")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    ticker: str
    label: str
    kind: str                    # "ENTRY" or "INVALIDATED"
    direction: str                # "LONG" or "SHORT"
    entry: float | None
    stop: float | None
    target: float | None
    rr_target: float
    breakout_level: float | None
    retest_level: float | None
    candle_time: str
    timestamp: str


# ---------------------------------------------------------------------------
# State (prevents duplicate alerts for a candle we've already alerted on)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read state file (%s) — starting fresh.", exc)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True, default=str)


def already_alerted(state: dict, ticker: str, candle_time: str) -> bool:
    return state.get(ticker) == candle_time


def mark_alerted(state: dict, ticker: str, candle_time: str) -> None:
    state[ticker] = candle_time


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def fetch_15m_candles(ticker: str) -> pd.DataFrame | None:
    """Pull ~60 days of 15m candles (yfinance's max at this resolution)."""
    try:
        data = yf.download(ticker, period="60d", interval="15m", progress=False)
    except Exception as exc:  # yfinance can raise a range of network/parse errors
        log.error("[%s] fetch failed: %s", ticker, exc)
        return None
    if data is None or data.empty:
        return data

    # Newer yfinance versions return MultiIndex columns (e.g. ('Open', 'AAPL'))
    # even for a single ticker. Flatten to plain column names either way.
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    data.columns = [str(c).lower() for c in data.columns]

    return data[["open", "high", "low", "close", "volume"]]


def drop_unclosed_candle(df: pd.DataFrame, interval_minutes: int = BAR_INTERVAL_MINUTES) -> pd.DataFrame:
    """
    yfinance timestamps a bar by its START time, so a bar is only actually
    closed once (start + interval) has passed. Signalling off a
    still-forming bar means alerting on data that can still change.
    """
    if df.empty:
        return df

    last_ts = df.index[-1]
    now = pd.Timestamp.now(tz=last_ts.tzinfo) if last_ts.tzinfo is not None else pd.Timestamp.now()

    if last_ts + pd.Timedelta(minutes=interval_minutes) > now:
        return df.iloc[:-1]
    return df


# ---------------------------------------------------------------------------
# Core strategy logic (pivots + retest state machine)
# ---------------------------------------------------------------------------

def find_pivots(df: pd.DataFrame, left: int = 5, right: int = 5) -> pd.DataFrame:
    """
    Marks a candle as a pivot high/low if its high/low is the most extreme
    within `left` candles before and `right` candles after it.
    NOTE: a pivot at index i is only *confirmed* once you reach index
    i + right (you need the future candles to know it held) — the signal
    loop below respects this lag, so there's no lookahead bias.
    """
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    pivot_high = np.zeros(n, dtype=bool)
    pivot_low = np.zeros(n, dtype=bool)

    for i in range(left, n - right):
        window_high = highs[i - left: i + right + 1]
        window_low = lows[i - left: i + right + 1]
        if highs[i] == window_high.max():
            pivot_high[i] = True
        if lows[i] == window_low.min():
            pivot_low[i] = True

    df = df.copy()
    df["pivot_high"] = pivot_high
    df["pivot_low"] = pivot_low
    return df


def generate_signals(
    df: pd.DataFrame,
    pivot_left: int = 7,
    pivot_right: int = 7,
    retest_window: int = 25,
    retest_tolerance: float = 0.15,
    volume_multiplier: float | None = None,
    volume_ma_window: int = 20,
) -> pd.DataFrame:
    df = find_pivots(df, pivot_left, pivot_right)
    n = len(df)

    # Some data sources (notably yfinance for forex pairs) don't provide
    # real volume at all. If the filter is on but the series is unusable
    # (all NaN or all zero), silently blocking every signal forever is
    # worse than just disabling the filter for this run.
    if volume_multiplier is not None:
        vol_series = df["volume"]
        volume_data_available = vol_series.notna().any() and (vol_series.fillna(0) != 0).any()
        if not volume_data_available:
            log.warning("No usable volume data in this dataset; disabling the volume filter for this run.")
            volume_multiplier = None

    # Rolling average volume, computed from PRIOR candles only (shifted by 1)
    # so the breakout candle is compared against history, not against itself.
    if volume_multiplier is not None:
        vol_ma = df["volume"].rolling(volume_ma_window).mean().shift(1)
    else:
        vol_ma = None

    signals = [None] * n  # "long_entry" / "short_entry" / "long_invalid" / "short_invalid"
    breakout_levels = [None] * n
    retest_levels = [None] * n

    last_pivot_high = last_pivot_high_idx = None
    last_pivot_low = last_pivot_low_idx = None
    prior_pivot_low_before_high = None  # swing low that preceded the current swing high
    prior_pivot_high_before_low = None  # swing high that preceded the current swing low

    long_state = None   # None | "broken" (waiting for retest)
    short_state = None
    long_breakout_level = long_retest_level = long_broken_at = None
    short_breakout_level = short_retest_level = short_broken_at = None

    for i in range(n):
        row = df.iloc[i]

        # Reveal pivots once confirmed (lag by `pivot_right` bars, no lookahead)
        confirm_idx = i - pivot_right
        if confirm_idx >= 0:
            if df["pivot_high"].iloc[confirm_idx]:
                prior_pivot_low_before_high = last_pivot_low
                last_pivot_high = df["high"].iloc[confirm_idx]
                last_pivot_high_idx = confirm_idx
            if df["pivot_low"].iloc[confirm_idx]:
                prior_pivot_high_before_low = last_pivot_high
                last_pivot_low = df["low"].iloc[confirm_idx]
                last_pivot_low_idx = confirm_idx

        # ---------------- LONG side ----------------
        if long_state is None and last_pivot_high is not None and prior_pivot_low_before_high is not None:
            volume_ok = True
            if volume_multiplier is not None:
                avg_vol = vol_ma.iloc[i]
                volume_ok = pd.notna(avg_vol) and row["volume"] >= volume_multiplier * avg_vol

            if row["close"] > last_pivot_high and volume_ok:
                leg_range = last_pivot_high - prior_pivot_low_before_high
                if leg_range > 0:
                    long_breakout_level = last_pivot_high
                    long_retest_level = last_pivot_high - 0.5 * leg_range
                    long_broken_at = i
                    long_state = "broken"

        elif long_state == "broken":
            bars_since = i - long_broken_at
            tol = retest_tolerance * (long_breakout_level - long_retest_level)
            zone_hi, zone_lo = long_retest_level + tol, long_retest_level - tol

            # Invalidation: closes back below the broken level (failed breakout)
            if row["close"] < long_retest_level - 2 * tol:
                signals[i] = "long_invalid"
                breakout_levels[i], retest_levels[i] = long_breakout_level, long_retest_level
                long_state = None

            # Touch the retest zone
            elif row["low"] <= zone_hi and row["low"] >= zone_lo - tol:
                # Confirmation candle: bullish close, back above the zone
                if row["close"] > row["open"] and row["close"] >= long_retest_level:
                    signals[i] = "long_entry"
                    breakout_levels[i], retest_levels[i] = long_breakout_level, long_retest_level
                    long_state = None  # reset, ready for next setup

            if bars_since > retest_window and long_state == "broken":
                long_state = None  # setup expired, no retest happened in time

        # ---------------- SHORT side ----------------
        if short_state is None and last_pivot_low is not None and prior_pivot_high_before_low is not None:
            volume_ok = True
            if volume_multiplier is not None:
                avg_vol = vol_ma.iloc[i]
                volume_ok = pd.notna(avg_vol) and row["volume"] >= volume_multiplier * avg_vol

            if row["close"] < last_pivot_low and volume_ok:
                leg_range = prior_pivot_high_before_low - last_pivot_low
                if leg_range > 0:
                    short_breakout_level = last_pivot_low
                    short_retest_level = last_pivot_low + 0.5 * leg_range
                    short_broken_at = i
                    short_state = "broken"

        elif short_state == "broken":
            bars_since = i - short_broken_at
            tol = retest_tolerance * (short_retest_level - short_breakout_level)
            zone_hi, zone_lo = short_retest_level + tol, short_retest_level - tol

            if row["close"] > short_retest_level + 2 * tol:
                signals[i] = "short_invalid"
                breakout_levels[i], retest_levels[i] = short_breakout_level, short_retest_level
                short_state = None

            elif row["high"] >= zone_lo and row["high"] <= zone_hi + tol:
                if row["close"] < row["open"] and row["close"] <= short_retest_level:
                    signals[i] = "short_entry"
                    breakout_levels[i], retest_levels[i] = short_breakout_level, short_retest_level
                    short_state = None

            if bars_since > retest_window and short_state == "broken":
                short_state = None

    df["signal"] = signals
    df["breakout_level"] = breakout_levels
    df["retest_level"] = retest_levels
    return df


def backtest(df: pd.DataFrame, rr_target: float = 2.0) -> pd.DataFrame:
    """
    Simple offline backtest utility (not used by run()): enter on signal,
    exit on stop or measured-move target. Stop = the retest extreme (low of
    the confirmation candle for longs, high of it for shorts). No
    slippage/fees modeled.
    """
    trades = []
    for i, row in df.iterrows():
        if row["signal"] not in ("long_entry", "short_entry"):
            continue

        entry = row["close"]
        if row["signal"] == "long_entry":
            stop = row["low"]
            risk = entry - stop
            if risk <= 0:
                continue
            target = entry + rr_target * risk
            direction = "long"
        else:
            stop = row["high"]
            risk = stop - entry
            if risk <= 0:
                continue
            target = entry - rr_target * risk
            direction = "short"

        outcome, exit_price, exit_idx = "open", None, None
        idx_pos = df.index.get_loc(i)
        for j in range(idx_pos + 1, len(df)):
            bar = df.iloc[j]
            if direction == "long":
                hit_stop = bar["low"] <= stop
                hit_target = bar["high"] >= target
            else:
                hit_stop = bar["high"] >= stop
                hit_target = bar["low"] <= target

            if hit_stop:
                outcome, exit_price = "stop", stop
            elif hit_target:
                outcome, exit_price = "target", target

            if outcome != "open":
                exit_idx = df.index[j]
                break

        pnl_r = None
        if outcome == "stop":
            pnl_r = -1.0
        elif outcome == "target":
            pnl_r = rr_target

        trades.append(
            {
                "entry_time": i,
                "direction": direction,
                "entry": entry,
                "stop": stop,
                "target": target,
                "outcome": outcome,
                "exit_time": exit_idx,
                "pnl_r": pnl_r,
            }
        )

    return pd.DataFrame(trades)


def load_csv(path: str) -> pd.DataFrame:
    """Offline utility for backtesting against an exported CSV (not used by run())."""
    df = pd.read_csv(path)
    df.columns = [c.lower() for c in df.columns]
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").set_index("timestamp")
    return df[["open", "high", "low", "close", "volume"]]


# ---------------------------------------------------------------------------
# Signal construction
# ---------------------------------------------------------------------------

def build_signal(cfg: dict, row: pd.Series, candle_time: str, signal_code: str) -> Signal:
    direction = "LONG" if "long" in signal_code else "SHORT"
    kind = "ENTRY" if "entry" in signal_code else "INVALIDATED"

    entry = stop = target = None
    if kind == "ENTRY":
        entry = float(row["close"])
        if direction == "LONG":
            stop = float(row["low"])
            risk = entry - stop
        else:
            stop = float(row["high"])
            risk = stop - entry
        if risk > 0:
            target = entry + cfg["rr_target"] * risk if direction == "LONG" else entry - cfg["rr_target"] * risk

    breakout_level = row.get("breakout_level")
    retest_level = row.get("retest_level")

    return Signal(
        ticker=cfg["ticker"],
        label=cfg["label"],
        kind=kind,
        direction=direction,
        entry=round(entry, 5) if entry is not None else None,
        stop=round(stop, 5) if stop is not None else None,
        target=round(target, 5) if target is not None else None,
        rr_target=cfg["rr_target"],
        breakout_level=round(float(breakout_level), 5) if pd.notna(breakout_level) else None,
        retest_level=round(float(retest_level), 5) if pd.notna(retest_level) else None,
        candle_time=candle_time,
        timestamp=datetime.utcnow().isoformat(timespec="seconds") + "Z",
    )


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------

def format_message(sig: Signal) -> str:
    levels_line = None
    if sig.breakout_level is not None and sig.retest_level is not None:
        levels_line = f"Breakout level: `{sig.breakout_level}`  |  Retest (50%) level: `{sig.retest_level}`"

    if sig.kind == "ENTRY":
        emoji = "\U0001F7E2" if sig.direction == "LONG" else "\U0001F534"
        risk_pts = abs(sig.entry - sig.stop)
        lines = [
            f"{emoji} *Breakout Retest 15m Signal — {sig.label}*",
            f"Direction: *{sig.direction}* (ENTRY)",
            f"Entry: `{sig.entry}`",
            f"Stop: `{sig.stop}`",
            f"Target ({sig.rr_target:g}R): `{sig.target}`",
            f"Risk: `{round(risk_pts, 2)}` pts",
        ]
    else:
        lines = [
            f"⚪ *Breakout Retest 15m Signal — {sig.label}*",
            f"Direction: *{sig.direction}* (INVALIDATED — no trade)",
        ]

    if levels_line:
        lines.append(levels_line)
    lines.append(f"Candle: {sig.candle_time}")
    return "\n".join(lines)


def send_telegram(message: str) -> None:
    if DRY_RUN:
        log.info("[dry-run] would send:\n%s", message)
        return
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set — printing instead of sending.")
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Telegram send failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> int:
    state = load_state()
    signals_sent = 0

    for cfg in SYMBOLS:
        ticker, label = cfg["ticker"], cfg["label"]

        df = fetch_15m_candles(ticker)
        if df is None or df.empty:
            log.info("[skip] %s: no data returned", label)
            continue

        df = drop_unclosed_candle(df)
        min_bars = cfg["pivot_left"] + cfg["pivot_right"] + 10
        if df.empty or len(df) < min_bars:
            log.info("[skip] %s: not enough closed candles yet", label)
            continue

        signals_df = generate_signals(
            df,
            pivot_left=cfg["pivot_left"],
            pivot_right=cfg["pivot_right"],
            retest_window=cfg["retest_window"],
            retest_tolerance=cfg["retest_tolerance"],
            volume_multiplier=cfg["volume_multiplier"],
            volume_ma_window=cfg["volume_ma_window"],
        )

        last_row = signals_df.iloc[-1]
        candle_time = str(signals_df.index[-1])
        signal_code = last_row["signal"]

        if pd.isna(signal_code):
            log.info("[skip] %s: no new signal at %s", label, candle_time)
            continue

        if already_alerted(state, ticker, candle_time):
            log.info("[skip] %s: signal already alerted for %s", label, candle_time)
            continue

        sig = build_signal(cfg, last_row, candle_time, signal_code)

        try:
            send_telegram(format_message(sig))
        except Exception:
            log.error("[%s] failed to deliver signal — will retry next run (state not marked).", label)
            continue

        mark_alerted(state, ticker, candle_time)
        signals_sent += 1
        log.info("[signal] %s: %s %s @ %s (stop %s, target %s)", label, sig.kind, sig.direction, sig.entry, sig.stop, sig.target)

    save_state(state)
    log.info("Run complete. %d signal(s) sent.", signals_sent)
    return 0


if __name__ == "__main__":
    sys.exit(run())
