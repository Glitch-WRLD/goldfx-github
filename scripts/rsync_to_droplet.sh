#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# Copy the GoldFX Agent project from THIS machine to a DigitalOcean droplet.
# Usage:
#     bash scripts/rsync_to_droplet.sh <droplet-ip>            # uses ~/.ssh/goldfx_droplet
#     SSH_KEY=id_ed25519 bash scripts/rsync_to_droplet.sh <ip> # custom key
# Copies code + .env (bot token); skips venv/cache/logs.
# Then SSH in and run:  cd goldfx-agent && bash scripts/deploy_pi.sh
# ----------------------------------------------------------------------------
set -euo pipefail

IP="${1:?usage: bash scripts/rsync_to_droplet.sh <droplet-ip>}"
KEY="${SSH_KEY:-$HOME/.ssh/goldfx_droplet}"
DEST="root@$IP"

[ -f "$KEY" ] || { echo "!! SSH key not found: $KEY"; exit 1; }

ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$DEST" "mkdir -p goldfx-agent"

rsync -avz --progress -e "ssh -i $KEY" \
  --exclude='.venv/' \
  --exclude='data_cache/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='bot.log' \
  --exclude='bot_state.json' \
  . "$DEST":goldfx-agent/

echo ""
echo "=== files copied. Now run on the droplet: ==="
echo "  ssh -i $KEY $DEST"
echo "  cd goldfx-agent && bash scripts/deploy_pi.sh"