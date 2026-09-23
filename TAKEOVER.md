# GOLDFX ARCHITECTURE & TAKEOVER HANDOFF

## 1. Serial Pipeline & Topology
- **GitHub**: Public repo `Glitch-WRLD/goldfx-github.git` on branch `main`.
  - GitHub Actions runs `.github/workflows/scanner.yml` -> `scripts/gha_scan.py`.
  - Scans the 7-pair dual-engine arsenal (Momentum FVG + Institutional SMC Sweep), delivers signals to Telegram chat, and commits state to `gha_state/state.json`.
- **Kali Relay**: Persistent systemd unit `goldfx-relay` running `/srv/goldfx-relay/relay.py` on port `:8123`.
  - Serves `http://192.168.52.128:8123/state.json`.
  - Fetches raw JSON from `https://raw.githubusercontent.com/Glitch-WRLD/goldfx-github/main/gha_state/state.json` every 12s with zero auth.
- **Windows Box (`C:\goldfx-agent`)**:
  - Secrets in `C:\goldfx-agent\.env` (MT5 creds, `AGENT_STATE_URL=http://192.168.52.128:8123/state.json`).
  - Auto-trade execution agent: `scripts/run_agent.py` (via `start_agent.bat`).
  - Polls Kali relay every 20s, sizes positions via `config.py` / `engine/risk.py`, executes them in MT5, and runs real-time position management.

## 2. Wiring Contract & Backtest Gate
- Upgrades (entry confirm, SL->BE, partial-close, Local TP Guard, smart reversal exits) must be verified against Headway MT5 candle backtests before deployment.
- Never override without a backtest that printed numbers.

## 3. Dead-Dial Ledger
| Date | Incident | Root Cause & Fix |
| :--- | :--- | :--- |
| 2026-09-21 | Relay crash-loop (box 10061 storm) | relay.py itself still hardcoded 4 `/tmp/state_relay` paths inside its own source (STATE_PATH, LOGFILE, os.makedirs, os.chdir) — `/tmp` is tmpfs on Kali, wiped on reboot -> FileHandler FileNotFoundError -> python status=1 -> systemd crash-loop -> box window 10061. FIX: rewrite the 4 internal constants to `/srv/goldfx-relay`, restart. VERIFIED: 0 `/tmp` refs in relay.py, unit active, :8123 LISTENING, box URL 200, served ref_seq=25. |
| 2026-09-22 | MT5 IPC Timeout (-10005) | Modal login dialog + `Api=0`/`Enabled=0` in common.ini + status loop expecting code 2. Fixed via `startup.ini`, `common.ini` fix, and RVA patch in `_core` to accept status 1. |
| 2026-09-23 | Broker TP Ask-Hover / Spread Miss | Broker TP on Short positions requires Ask to reach TP. When Bid crosses TP but Ask is 0.1-0.8 pips wide, price turns without triggering broker TP. Fixed via `Local TP Guard` in `agent/watcher.py` (market-closes immediately when Bid <= TP). |
| 2026-09-23 | Signal Telegram Stagnation | Local git was 6 commits ahead of origin/main due to expired GCM auth token. Pushed with Personal Access Token to resume GHA delivery. |
