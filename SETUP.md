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
- `breakout_retest_strategy.py` — the strategy logic (unchanged, already validated bar-for-bar against a fast NumPy engine used for the optimization sweep)
- `live_alert_bot.py` — the live polling bot. Validated parameters are baked in as defaults (pivot=7, retest_window=25, tolerance=0.15, rr_target=2.5, all-hours). Fetches ES=F/NQ=F via yfinance, checks for a new closed 15m candle, alerts on new signals, tracks state in `alert_state.json` so it never double-alerts.
- `.github/workflows/alerts.yml` — runs `live_alert_bot.py --once` every 15 minutes on weekdays via GitHub Actions cron, commits the updated `alert_state.json` back to the repo so state persists between runs.
- `requirements.txt` — pandas, numpy, requests, yfinance.

## 4. Turn it on
1. Push these files to your repo (or update the existing ones — see diff notes below).
2. Go to the **Actions** tab → "Breakout Retest Alerts" workflow → **Enable workflow** if it's not already.
3. Optionally trigger it manually once (Actions → workflow → "Run workflow") to confirm it fires a real Telegram message.
4. It then runs automatically every 15 minutes, Mon-Fri, and pushes `alert_state.json` updates back to `main`.

## 5. Test locally before trusting it live
```bash
pip install -r requirements.txt
python live_alert_bot.py --tickers ES=F,NQ=F --once --dry-run
```
This prints what it *would* alert without sending anything — do this first.

## What changed vs. the original repo version
- Strategy parameters switched from untested defaults to the ones that
  survived a 540-combo optimization sweep + in-sample/out-of-sample check
  on ~5-6 months of real ES/NQ data (see `OPTIMIZATION_REPORT.md` and
  `COMBINED_ES_NQ_REPORT.md` in this repo for the full study).
- Alerts now show entry/stop/target/$ risk-reward, not just OHLC.
- Ticker list narrowed to ES=F,NQ=F — the only two instruments actually
  backtested. (MYM=F, EURUSD=X, GBPJPY=X from the old workflow were never
  validated for this strategy — remove or re-validate before re-adding them.)

## Read before trusting this with real money
This strategy's edge, even in its best validated form, is modest (see the
reports) and concentrated mostly in NQ, on a single ~5-6 month sample. Alert
and paper-trade for a while before sizing up. Re-run the optimization sweep
periodically as new data comes in — see the "Notes on going further" comment
block at the bottom of `live_alert_bot.py`.
