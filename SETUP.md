# Setup: GitHub + Telegram automation for the breakout-retest alert bot

## 1. Create a Telegram bot
1. Message **@BotFather** on Telegram, send `/newbot`, follow the prompts.
2. BotFather gives you a **bot token** (looks like `123456789:AAExampleTokenHere`).
3. Message your new bot anything (so it can see your chat), then visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and find
   your **chat id** in the JSON response (`"chat":{"id": ...}`).

## 2. Add secrets to your GitHub repo
Repo → Settings → Secrets and variables → Actions → New repository secret:
- `TELEGRAM_TOKEN` = the bot token from step 1
- `TELEGRAM_CHAT_ID` = your chat id from step 1

## 3. Files in this repo
- `breakout_retest_signal_bot.py` — the whole bot: strategy logic (pivots + retest state machine, already validated), live polling, and Telegram delivery, in one file, matching the format of the companion ORB signal bot. Validated parameters and the ES=F/NQ=F symbol list are configured in the `SYMBOLS` list at the top of the file (pivot=7, retest_window=25, tolerance=0.15, rr_target=2.5, all-hours). Checks for a new closed 15m candle, alerts on new signals, tracks state in `alert_state.json` so it never double-alerts.
- `.github/workflows/alerts.yml` — runs `breakout_retest_signal_bot.py` every 15 minutes on weekdays via GitHub Actions cron, commits the updated `alert_state.json` back to the repo so state persists between runs. Supports a `dry_run` input when triggered manually.
- `requirements.txt` — pandas, numpy, requests, yfinance.

## 4. Turn it on
1. Push these files to your repo (or update the existing ones — see diff notes below).
2. Go to the **Actions** tab → "Breakout Retest Alerts" workflow → **Enable workflow** if it's not already.
3. Optionally trigger it manually once (Actions → workflow → "Run workflow", `dry_run: true`) to confirm it fires a real Telegram message.
4. It then runs automatically every 15 minutes, Mon-Fri, and pushes `alert_state.json` updates back to `main`.

## 5. Test locally before trusting it live
```bash
pip install -r requirements.txt
DRY_RUN=1 TELEGRAM_TOKEN=xxxx TELEGRAM_CHAT_ID=xxxx python breakout_retest_signal_bot.py
```
This logs what it *would* alert without sending anything — do this first.
To change symbols or strategy parameters, edit the `SYMBOLS` list at the top
of `breakout_retest_signal_bot.py` — there's no CLI flag for it.

## What changed vs. the original repo version
- Strategy parameters switched from untested defaults to the ones that
  survived a 540-combo optimization sweep + in-sample/out-of-sample check
  on ~5-6 months of real ES/NQ data (see `OPTIMIZATION_REPORT.md` and
  `COMBINED_ES_NQ_REPORT.md` in this repo for the full study).
- Alerts now show entry/stop/target/risk-reward in points, not just OHLC.
- Ticker list narrowed to ES=F,NQ=F — the only two instruments actually
  backtested. (MYM=F, EURUSD=X, GBPJPY=X from the old workflow were never
  validated for this strategy.)
- Merged the old two-file layout (`breakout_retest_strategy.py` +
  `live_alert_bot.py`) into a single `breakout_retest_signal_bot.py`,
  driven entirely by environment variables and the `SYMBOLS` config list
  instead of CLI flags — matching the format of the companion ORB signal
  bot (`cronus-orb-live`). The offline `backtest()`/`load_csv()` helpers
  are still in the file as importable functions, just without the old
  `--csv` CLI entry point.

## Read before trusting this with real money
This strategy's edge, even in its best validated form, is modest (see the
reports) and concentrated mostly in NQ, on a single ~5-6 month sample. Alert
and paper-trade for a while before sizing up. Re-run the optimization sweep
periodically as new data comes in — see the "Notes on going further" comment
block at the bottom of `live_alert_bot.py`.
