#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# Copy the GoldFX Agent project from THIS machine to your Raspberry Pi.
# Usage:
#     bash scripts/rsync_to_pi.sh pi@raspberrypi
#     bash scripts/rsync_to_pi.sh pi@192.168.1.50
# (first arg defaults to pi@raspberrypi.local)
#
# Copies code + .env (your bot token), skips the virtualenv, cached candles
# (re-downloaded on first run on the Pi) and local logs/state.
# Then SSH in and run:  cd goldfx-agent && bash scripts/deploy_pi.sh
# ----------------------------------------------------------------------------
set -euo pipefail

DEST="${1:-pi@raspberrypi.local}"

ssh "$DEST" "mkdir -p goldfx-agent"

rsync -avz --progress \
  --exclude='.venv/' \
  --exclude='data_cache/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='bot.log' \
  --exclude='bot_state.json' \
  . "$DEST":goldfx-agent/

echo ""
echo "=== files copied. Now on the Pi run: ==="
echo "  ssh $DEST"
echo "  cd goldfx-agent && bash scripts/deploy_pi.sh"