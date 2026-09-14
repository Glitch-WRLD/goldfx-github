# GoldFX Agent

Telegram signal bot for **XAU/USD** and **EUR/USD** on low timeframes
(M15 / M30 / H1 / H2). Analyzes TradingView candle data, detects a vetted
FVG-retest strategy in the direction of the higher-timeframe bias, and drops
tradable entries (entry / SL / TP / lot size) to your Telegram chat.

## What was built and why

| Stage | Result |
|---|---|
| Data | TradingView OHLC via `tradingview-sdk[pandas]`, cached as parquet in `data_cache/` |
| Strategy | 6 candidates backtested; naive entries bleed on range-bound EURUSD. Winner: **FVG retest + HTF EMA50 bias + structural TPs** |
| Validation | 60/40 chronological walk-forward, monthly R-series, net of spread/slippage |
| Profiles | `hi` high-win-rate (~57%) and `balanced` (PF ~1.5) - see `reports/COMPARISON.md` |
| Delivery | Async scanner loop - live signal per closed bar, deduped, pushed to Telegram |

### Backtest hubs (net of costs, Jan-2025 .. Sep-2026)

| Profile | n | WR | avgR | PF | totalR | maxDD | positive months |
|---|---|---:|---:|---:|---:|---:|---:|
| High Win-Rate | 1947 | 57.7% | +0.15 | 1.36 | +299 | 47R | 15/21 |
| Balanced | 2957 | 49.6% | +0.24 | 1.48 | +711 | 65R | 17/21 |

Both remain positive out-of-sample. Honest caveats: optimized on gold's 2026
trend; EURUSD-only cells are the weak spots. **Paper trade before live.**

## Setup

```bash
cd goldfx-agent
python -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# fill BOT_TOKEN (BotFather) and CHAT_ID (see .env.example)
```

## Run (local)

```bash
# download/refresh candle cache for both pairs (M15..H2)
.venv/bin/python scripts/download_data.py

# re-run the backtest comparison + validation
.venv/bin/python scripts/validate_final.py
.venv/bin/python scripts/gen_report.py

# start the bot (scanner loop + commands)
.venv/bin/python scripts/run_bot.py
```

## Run on a Raspberry Pi (24/7)

The Pi keeps the bot alive across reboots via systemd. Use a **64-bit**
Raspberry Pi OS (arm64) — pandas/pyarrow provide ARM wheels for it.

1. From this machine, copy the project over LAN:
   ```bash
   bash scripts/rsync_to_pi.sh pi@raspberrypi.local   # or pi@<ip>
   ```
   (Copies code + `.env`; skips venv/cache/logs — they re-create on the Pi.)
2. On the Pi:
   ```bash
   ssh pi@raspberrypi.local
   cd goldfx-agent && bash scripts/deploy_pi.sh
   ```
   The script installs deps, registers `goldfx` as a systemd service
   (auto-restart on crash + boot) and starts it. Check it with:
   ```bash
   journalctl -u goldfx -f
   sudo systemctl status goldfx
   ```

## Telegram commands

```
/start          welcome + first scan
/status         scanner health, profile, risk config
/scannow        force a scan of both pairs now
/bias           current HTF bias per pair
/setprofile hi | balanced
/history        last signals (deduped)
/help
```

With `CHAT_ID` set, the bot pushes signals automatically (dedupe per bar,
state persisted in `bot_state.json`).

## Strategy summary

1. **Fair Value Gap**: 3-candle imbalance (`high[2] < low` / `low[2] > high`).
2. **HTF bias filter** (EMA50 on the resampled higher timeframe): XAUUSD → H1,
   EURUSD → H2. Longs only in bullish bias (configurable).
3. **Entry**: price retests the gap zone; optional confirmation bar (`confirm`).
4. **SL**: below/above recent structure. **TP**: structural level from the
   smart-TP model, capped to the profile's R:R band.

Live sizing (defaults): risk 1% of balance per trade (2% cap), daily loss
limit 3%, circuit breaker after 4 straight losses, quarter-Kelly cap.
Contract math is baked into `engine/risk.py` (XAU 100 oz/lot etc.).

## Layout

```
config.py                 env + constants (symbols, risk, contracts)
data/tv_data.py           TradingView fetch/cache (restores DatetimeIndex)
strategy/indicators.py    EMA/RSI/ATR/Bollinger/swings...
strategy/backtester.py    Signal/Trade/BacktestResult + run_backtest
strategy/candidates.py    6 strategies + smart-TP/bias helpers
strategy/filters.py       htf_bias + numpy FVG detector
strategy/profiles.py      tuned HI/BAL params + per-symbol runtime TFs
engine/risk.py            sizing, Kelly cap, circuit breakers
engine/scanner.py         live FVG scanner + signal formatting
engine/state.py           bot_state.json (dedupe/history/profile)
bot/telegram_bot.py       async bot + scanner loop + commands
pinescript/               TradingView arrows/zones overlay (visual parity)
deploy/goldfx.service     systemd unit template for the Raspberry Pi
scripts/                  backtest, validation, report, bot, rsync_to_pi, deploy_pi
reports/                  sweep_results.json, final_validation.json, COMPARISON.md
```

## Free 24/7 hosting via GitHub Actions (no server, no card)

The project ships a scheduler runner (`.github/workflows/scanner.yml` +
`scripts/gha_scan.py`) that re-scans on a cron and drops signals to Telegram —
for free, forever, with no VM. Key points:

- **Public repo = unlimited Actions minutes** (private repos cap at 2,000
  min/month, which would force an hourly cadence). No secrets live in the repo:
  `BOT_TOKEN` / `CHAT_ID` go in *repository secrets*.
- Each tick: fetch last candles from TradingView → re-scan a trailing window of
  closed bars (catch-up: bars between ticks are delivered late, never missed) →
  send new signals → process `/commands` via `getUpdates` (replies lag up to one
  interval) → autocommit `gha_state/state.json` so the next tick continues.

**Setup (one-time):**

1. Create a public repo (e.g. `goldfx-agent`) on github.com, then push:
   ```bash
   cd goldfx-agent
   git remote add origin git@github.com:<YOUR_USER>/goldfx-agent.git
   git push -u origin main
   ```
   (Add the `goldfx-github` SSH key at github.com/settings/keys first.)
2. Repo → Settings → Secrets and variables → Actions → **New repository secret**:
   - `BOT_TOKEN` = your bot token
   - `CHAT_ID` = `-1004489143176`
3. **Stop this machine's local bot** so two processes don't poll the same token
   (`kill <pid>` / see `bot.log`).
4. Actions tab → `goldfx-scan` → Run workflow (first run), then it runs every
   15 min automatically.

Monitor ticks in the Actions tab; debug with `python scripts/gha_scan.py --no-updates`
locally (reads `.env`, doesn't touch `getUpdates`).

## Disclaimer

Educational research with backtest-tested but *not* live-tested performance.
Forex/CFDs are leveraged and risky; the bot does not execute trades. Confirm
quotes with your broker.