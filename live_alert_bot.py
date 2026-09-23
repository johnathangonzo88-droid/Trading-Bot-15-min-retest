"""
Live Alert Bot v2 — Breakout + 50% Retest, ES/NQ, Telegram
=============================================================
Runs the breakout-retest strategy against live/recent 15-min data on a
schedule (designed to run every 15 minutes via GitHub Actions cron) and
sends a Telegram alert the instant a new signal fires, with the actual
entry/stop/target levels so you know what to do with it.

WHAT CHANGED FROM v1 (live_alert_bot.py in the original repo):
  - Default strategy parameters are now the ones that survived a 540-combo
    grid search + in-sample/out-of-sample validation on ES and NQ 15m data
    (see OPTIMIZATION_REPORT.md / COMBINED_ES_NQ_REPORT.md in this repo):
        pivot_left = pivot_right = 7
        retest_window           = 25
        retest_tolerance        = 0.15
        rr_target               = 2.5   (used to compute the alert's stop/target, matching backtest_v2.py)
        session filter          = none (all hours) — RTH filtering left too little
                                   out-of-sample data to validate and is NOT used here
    The old defaults (pivot=5, window=20, tolerance=0.10) were never
    validated — they were just what happened to be in the original repo.
    IMPORTANT: this is a "didn't fall apart out-of-sample on ~5-6 months of
    data" result, not a proven edge. Re-validate periodically; see the
    caveats at the bottom of this file.
  - Alerts now include entry price, computed stop, computed target (using
    rr_target), risk in points and in $ (using each symbol's contract
    multiplier), so the alert is actionable without re-deriving it by hand.
  - Multi-symbol aware of contract specs (ES vs NQ point values) instead of
    a flat "close/high/low" dump.
  - Same polling/state-file/dedup design as v1 — still POLLING, not
    streaming; still ALERT-ONLY, does not place trades.

Data source: yfinance. This is fine when run from GitHub Actions (full
internet access on the runner) even though it's blocked in some sandboxed
dev environments — if you're testing this locally in a locked-down
container and yfinance fails to install/fetch, that's the environment, not
this script; it will work fine in the Actions runner.

IMPORTANT — before using this with real money:
  - This sends ALERTS ONLY. It does not place trades. Wiring it to
    auto-execute is a separate, much higher-stakes step (see notes at
    the bottom of this file).
  - yfinance's intraday data has a real-time lag and limited history
    (~60 days at 15m resolution) and is not suitable for latency-sensitive
    trading; swap in a broker/exchange API (Databento, Tradovate, etc.)
    for anything production-grade.
  - Always run with --dry-run first to confirm the alerts look right
    before pointing it at a real Telegram destination.

Usage examples:
  # One-off check (what the GitHub Actions workflow runs every 15 min):
  python live_alert_bot.py --tickers ES=F,NQ=F --once --dry-run

  # Real Telegram alerts, validated params (the defaults — no flags needed):
  python live_alert_bot.py --tickers ES=F,NQ=F --once \
      --telegram-token YOUR_BOT_TOKEN --telegram-chat-id YOUR_CHAT_ID

  # Long-running loop instead of cron:
  python live_alert_bot.py --tickers ES=F,NQ=F \
      --telegram-token YOUR_BOT_TOKEN --telegram-chat-id YOUR_CHAT_ID
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone

import pandas as pd

from breakout_retest_strategy import generate_signals, load_yfinance


STATE_FILE_DEFAULT = "alert_state.json"
BAR_INTERVAL_MINUTES = 15

# Contract specs for the alert's $ risk/reward math. Add symbols here as needed.
# Keyed by the yfinance ticker so --tickers ES=F,NQ=F "just works".
CONTRACT_SPECS = {
    "ES=F":  {"name": "E-mini S&P 500",   "multiplier": 50.0, "tick_size": 0.25},
    "NQ=F":  {"name": "E-mini Nasdaq-100", "multiplier": 20.0, "tick_size": 0.25},
    "MES=F": {"name": "Micro E-mini S&P 500",   "multiplier": 5.0, "tick_size": 0.25},
    "MNQ=F": {"name": "Micro E-mini Nasdaq-100", "multiplier": 2.0, "tick_size": 0.25},
}


# ----------------------------------------------------------------------
# Drop the currently-forming candle, if the fetch landed mid-bar.
# yfinance timestamps a bar by its START time, so a bar is only
# actually closed once (start + interval) has passed. Signalling off
# a still-forming bar means alerting on data that can still change.
# ----------------------------------------------------------------------
def drop_unclosed_candle(df: pd.DataFrame, interval_minutes: int = BAR_INTERVAL_MINUTES) -> pd.DataFrame:
    if df.empty:
        return df

    last_ts = df.index[-1]
    now = pd.Timestamp.now(tz=last_ts.tzinfo) if last_ts.tzinfo is not None else pd.Timestamp.now()

    if last_ts + pd.Timedelta(minutes=interval_minutes) > now:
        return df.iloc[:-1]
    return df


# ----------------------------------------------------------------------
# State persistence — tracks the last alerted candle per ticker so we
# never send the same signal twice across runs.
# ----------------------------------------------------------------------
def load_state(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_state(path: str, state: dict) -> None:
    with open(path, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ----------------------------------------------------------------------
# Alert delivery
# ----------------------------------------------------------------------
def format_alert(ticker: str, timestamp, signal: str, row: pd.Series, args) -> str:
    direction = "LONG" if "long" in signal else "SHORT"
    kind = "ENTRY" if "entry" in signal else "INVALIDATED"

    spec = CONTRACT_SPECS.get(ticker, {"name": ticker, "multiplier": 1.0, "tick_size": 0.01})
    header = f"[{kind}] {ticker} ({spec['name']}) — {direction}"

    lines = [header, f"Time: {timestamp}"]

    if kind == "ENTRY":
        entry = row["close"]
        # Same stop/target convention as backtest_v2.py: stop = the
        # confirmation candle's extreme, target = entry +/- rr_target * risk.
        if direction == "LONG":
            stop = row["low"]
            risk_pts = entry - stop
            target = entry + args.rr_target * risk_pts if risk_pts > 0 else None
        else:
            stop = row["high"]
            risk_pts = stop - entry
            target = entry - args.rr_target * risk_pts if risk_pts > 0 else None

        lines.append(f"Entry:  {entry:.2f}")
        lines.append(f"Stop:   {stop:.2f}  ({risk_pts:.2f} pts risk)" if risk_pts > 0 else "Stop:   n/a (invalid risk, skip this one)")
        if target is not None:
            lines.append(f"Target: {target:.2f}  ({args.rr_target:.1f}R)")
            risk_dollars = risk_pts * spec["multiplier"]
            reward_dollars = (target - entry) * spec["multiplier"] if direction == "LONG" else (entry - target) * spec["multiplier"]
            lines.append(f"Risk: ${risk_dollars:,.2f}/contract | Reward: ${reward_dollars:,.2f}/contract (1 contract, before commission/slippage)")
    else:
        lines.append(f"Close: {row['close']:.2f} | High: {row['high']:.2f} | Low: {row['low']:.2f}")
        lines.append("Setup invalidated before confirming — no trade.")

    lines.append(f"Signal code: {signal}")
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, message: str) -> None:
    import requests

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=10)
    resp.raise_for_status()


def send_discord(webhook_url: str, message: str) -> None:
    import requests

    resp = requests.post(webhook_url, json={"content": message}, timeout=10)
    resp.raise_for_status()


def dispatch_alert(message: str, args) -> None:
    if args.dry_run:
        print("---- DRY RUN ALERT ----")
        print(message)
        print("-----------------------")
        return
    if args.telegram_token and args.telegram_chat_id:
        send_telegram(args.telegram_token, args.telegram_chat_id, message)
    if args.discord_webhook:
        send_discord(args.discord_webhook, message)
    if not (args.telegram_token or args.discord_webhook):
        # No destination configured — fall back to printing so nothing is lost.
        print(message)


# ----------------------------------------------------------------------
# Core check: fetch data, generate signals, alert on anything new
# ----------------------------------------------------------------------
def check_ticker(ticker: str, state: dict, args) -> None:
    try:
        data = load_yfinance(ticker)
    except Exception as e:
        print(f"[{ticker}] failed to fetch data: {e}")
        return

    data = drop_unclosed_candle(data)

    if data.empty or len(data) < (args.pivot_left + args.pivot_right + 10):
        print(f"[{ticker}] not enough data yet, skipping")
        return

    signals_df = generate_signals(
        data,
        pivot_left=args.pivot_left,
        pivot_right=args.pivot_right,
        retest_window=args.retest_window,
        retest_tolerance=args.retest_tolerance,
        volume_multiplier=args.volume_multiplier,
        volume_ma_window=args.volume_ma_window,
    )

    last_row = signals_df.iloc[-1]
    last_ts = str(signals_df.index[-1])
    signal = last_row["signal"]

    last_alerted = state.get(ticker)

    if pd.notna(signal) and last_alerted != last_ts:
        message = format_alert(ticker, last_ts, signal, last_row, args)
        dispatch_alert(message, args)
        state[ticker] = last_ts  # remember we've alerted this candle
    else:
        print(f"[{ticker}] no new signal at {last_ts} (latest signal: {signal})")


def run_once(tickers, args) -> None:
    state = load_state(args.state_file)
    for ticker in tickers:
        check_ticker(ticker, state, args)
    save_state(args.state_file, state)


def run_loop(tickers, args) -> None:
    print(f"Starting loop, checking every {args.interval_minutes} minutes. Ctrl+C to stop.")
    while True:
        run_once(tickers, args)
        time.sleep(args.interval_minutes * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live breakout-retest alert bot (ES/NQ, Telegram/Discord)")
    parser.add_argument("--tickers", type=str, required=True, help="Comma-separated yfinance tickers, e.g. ES=F,NQ=F")
    parser.add_argument("--once", action="store_true", help="Run a single check and exit (use with cron/GitHub Actions)")
    parser.add_argument("--interval-minutes", type=int, default=15)
    parser.add_argument("--state-file", type=str, default=STATE_FILE_DEFAULT)
    parser.add_argument("--dry-run", action="store_true", help="Print alerts instead of sending them")

    parser.add_argument("--telegram-token", type=str, default=None)
    parser.add_argument("--telegram-chat-id", type=str, default=None)
    parser.add_argument("--discord-webhook", type=str, default=None)

    # Strategy params — defaults are the validated ES+NQ config from
    # OPTIMIZATION_REPORT.md / COMBINED_ES_NQ_REPORT.md, not the original
    # repo's untested defaults. Override via flags if you re-validate and
    # want to try something else.
    parser.add_argument("--pivot-left", type=int, default=7)
    parser.add_argument("--pivot-right", type=int, default=7)
    parser.add_argument("--retest-window", type=int, default=25)
    parser.add_argument("--retest-tolerance", type=float, default=0.15)
    parser.add_argument("--rr-target", type=float, default=2.5,
                         help="Used only to compute the alert's displayed target — the entry signal itself doesn't depend on this")
    parser.add_argument(
        "--volume-multiplier",
        type=float,
        default=None,
        help="Require breakout volume >= this multiple of the rolling average (e.g. 1.5). Omit to disable (default — matches the validated backtest, which had no usable volume data).",
    )
    parser.add_argument("--volume-ma-window", type=int, default=20)

    args = parser.parse_args()
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]

    if args.once:
        run_once(tickers, args)
    else:
        run_loop(tickers, args)

# ----------------------------------------------------------------------
# Notes on going further
# ----------------------------------------------------------------------
# 1. Data source: yfinance is free but not real-time-grade (delay, rate
#    limits, occasional gaps) and its 15m history only goes back ~60
#    days. The validated backtest used ~5-6 months of TradingView-exported
#    data — if you want to re-validate periodically, export fresh CSVs
#    from TradingView (Table view -> Download data) rather than relying on
#    yfinance's short window, and re-run the sweep in
#    OPTIMIZATION_REPORT.md's scripts.
#
# 2. Deployment: `--once` mode is meant to be triggered by the
#    accompanying GitHub Actions workflow (.github/workflows/alerts.yml)
#    every 15 minutes. That's simpler and more robust than a long-running
#    loop on a VPS, and it's already wired up in this repo — see that file.
#
# 3. Session filter: the validated config trades ALL HOURS (no RTH
#    restriction) because restricting to regular trading hours cut the
#    out-of-sample sample too thin to validate. If you add an RTH filter
#    back in, re-run the in-sample/out-of-sample check before trusting it.
#
# 4. Re-validation cadence: the whole point of OPTIMIZATION_REPORT.md's
#    warnings was that most parameter combinations that look good on one
#    sample stop working out-of-sample. Set a calendar reminder to re-run
#    the sweep every 1-2 months as new data comes in, and watch this
#    bot's own live alert_state.json / your actual paper-trade results for
#    drift away from the backtested win rate.
#
# 5. Auto-execution: turning an alert into an actual order is a
#    meaningfully bigger step — it means holding API keys with trading
#    permission, handling partial fills/rejections, and taking on much
#    more liability if the strategy misbehaves. Keep alerts and
#    execution as separate, deliberately-connected systems rather than
#    merging them by default.
