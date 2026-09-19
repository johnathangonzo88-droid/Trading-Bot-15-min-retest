"""
Live Alert Bot
===============
Runs the breakout-retest strategy against live/recent data on a schedule
and sends an alert (Telegram and/or Discord) the moment a new signal fires.

This is a POLLING design, not a streaming one: on each run it re-fetches a
recent window of 15m candles, re-runs the full signal generator over that
window (cheap enough at this data size), and only alerts on the *most
recent* candle if it produced a new signal that hasn't been alerted yet.
State (what's already been alerted) is persisted to a JSON file so you
can run this via cron every 15 minutes without duplicate alerts, or as a
long-running loop.

IMPORTANT — before using this with real money:
  - This sends ALERTS ONLY. It does not place trades. Wiring it to
    auto-execute is a separate, much higher-stakes step (see notes at
    the bottom of this file).
  - yfinance's 15m data has a real-time lag and is not suitable for
    latency-sensitive trading; swap in a broker/exchange API
    (Alpaca, Polygon, Binance, etc.) for anything production-grade.
  - Always run with --dry-run first to confirm the alerts look right
    before pointing it at a real Telegram/Discord destination.

Usage examples:
  # One-off check (good for a cron job running every 15 min), dry run:
  python live_alert_bot.py --tickers AAPL,MSFT --once --dry-run

  # Long-running loop, sending real Telegram alerts:
  python live_alert_bot.py --tickers AAPL,MSFT,TSLA \
      --telegram-token YOUR_BOT_TOKEN --telegram-chat-id YOUR_CHAT_ID

  # Discord instead:
  python live_alert_bot.py --tickers BTC-USD \
      --discord-webhook https://discord.com/api/webhooks/xxx/yyy
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone

import pandas as pd

from breakout_retest_strategy import generate_signals, load_yfinance


STATE_FILE_DEFAULT = "alert_state.json"


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
def format_alert(ticker: str, timestamp, signal: str, row: pd.Series) -> str:
    direction = "LONG" if "long" in signal else "SHORT"
    kind = "ENTRY" if "entry" in signal else "INVALIDATED"
    return (
        f"[{kind}] {ticker} {direction}\n"
        f"Time: {timestamp}\n"
        f"Close: {row['close']:.4f} | High: {row['high']:.4f} | Low: {row['low']:.4f}\n"
        f"Signal: {signal}"
    )


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
        message = format_alert(ticker, last_ts, signal, last_row)
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
    parser = argparse.ArgumentParser(description="Live breakout-retest alert bot")
    parser.add_argument("--tickers", type=str, required=True, help="Comma-separated tickers, e.g. AAPL,MSFT")
    parser.add_argument("--once", action="store_true", help="Run a single check and exit (use with cron)")
    parser.add_argument("--interval-minutes", type=int, default=15)
    parser.add_argument("--state-file", type=str, default=STATE_FILE_DEFAULT)
    parser.add_argument("--dry-run", action="store_true", help="Print alerts instead of sending them")

    parser.add_argument("--telegram-token", type=str, default=None)
    parser.add_argument("--telegram-chat-id", type=str, default=None)
    parser.add_argument("--discord-webhook", type=str, default=None)

    # Strategy params (mirror breakout_retest_strategy.py)
    parser.add_argument("--pivot-left", type=int, default=5)
    parser.add_argument("--pivot-right", type=int, default=5)
    parser.add_argument("--retest-window", type=int, default=20)
    parser.add_argument("--retest-tolerance", type=float, default=0.10)
    parser.add_argument("--volume-multiplier", type=float, default=1.5)
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
#    days. For a real product, swap load_yfinance() for a call to
#    Alpaca, Polygon, or your exchange's REST API — the rest of this
#    file doesn't need to change, just what feeds `data`.
#
# 2. Deployment: `--once` mode is meant to be triggered by cron every
#    15 minutes (aligned to candle close), which is simpler and more
#    robust than a long-running loop on a VPS. Example crontab entry
#    (adjust path and add */15 alignment to your market's timezone):
#      */15 * * * * cd /path/to/bot && python3 live_alert_bot.py \
#        --tickers AAPL,MSFT --once --telegram-token XXX --telegram-chat-id YYY
#
# 3. Subscriber gating: this sends to a single chat/webhook. To serve
#    paying subscribers, you'd add a layer that maps each alert to a
#    Telegram channel/Discord role gated by subscription status
#    (e.g. checked via a Stripe webhook updating a small database).
#
# 4. Auto-execution: turning an alert into an actual order is a
#    meaningfully bigger step — it means holding API keys with trading
#    permission, handling partial fills/rejections, and taking on much
#    more liability if the strategy misbehaves. Keep alerts and
#    execution as separate, deliberately-connected systems rather than
#    merging them by default.
