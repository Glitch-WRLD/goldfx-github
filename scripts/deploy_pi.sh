#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# GoldFX Agent - Raspberry Pi 24/7 deploy script.
# Run this ON the Pi, inside the project folder:
#     cd goldfx-agent && bash scripts/deploy_pi.sh
# It installs deps, creates .env if missing, and registers the bot as a
# systemd service that restarts on boot / crashes automatically.
# ----------------------------------------------------------------------------
set -euo pipefail

PROJDIR="$(pwd)"
SERVICE=goldfx
UNIT="deploy/goldfx.service"

# --- user vs root (droplets log in as root and have no sudo password) ------
if [ "$(id -u)" = "0" ]; then
  SUDO=""
else
  SUDO="sudo"
  command -v sudo >/dev/null || { echo "!! sudo not found - run as root or a sudo-capable user"; exit 1; }
fi

# --- sanity: 64-bit OS? -----------------------------------------------------
arch="$(dpkg --print-architecture)"
echo ">> architecture: $arch"
if [ "$arch" != "arm64" ]; then
  echo "!! WARNING: your OS reports '$arch'."
  echo "   pandas/pyarrow ship ARM wheels for 64-bit (arm64) Raspberry Pi OS."
  echo "   On a 32-bit (armhf) system, 'pip install pyarrow' will likely fail."
  echo "   Recommended: re-flash with Raspberry Pi OS 64-bit."
fi

# --- system deps ------------------------------------------------------------
echo ">> installing system packages (sudo may prompt)..."
$SUDO apt-get update -y
$SUDO apt-get install -y python3-venv python3-pip ca-certificates

# --- python env -------------------------------------------------------------
if [ ! -d .venv ]; then
  echo ">> creating virtualenv..."
  python3 -m venv .venv
fi
echo ">> upgrading pip + installing requirements..."
.venv/bin/pip install --upgrade pip wheel setuptools
.venv/bin/pip install -r requirements.txt

# --- .env -------------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  echo "!! created .env from .env.example"
  echo "   PASTE your BOT_TOKEN and CHAT_ID into .env now, then re-run this script."
  exit 1
fi
grep -q "your_token_here" .env && {
  echo "!! BOT_TOKEN in .env is still the placeholder. Edit .env first."
  exit 1
}
echo ">> .env OK (token present)"

# --- systemd service --------------------------------------------------------
echo ">> installing systemd unit..."
USER_NAME="$(id -un)"
sed -e "s|__USER__|$USER_NAME|g" \
    -e "s|__PROJDIR__|$PROJDIR|g" \
    -e "s|__VENVPY__|$PROJDIR/.venv/bin/python|g" \
    "$UNIT" | $SUDO tee /etc/systemd/system/"$SERVICE.service" >/dev/null

$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE"
$SUDO systemctl restart "$SERVICE"

sleep 6
echo ""
echo ">> service status:"
systemctl --no-pager --lines=15 status "$SERVICE" || true

echo ""
echo "=== done ==="
echo "  live log:    journalctl -u $SERVICE -f"
echo "  restart:     sudo systemctl restart $SERVICE"
echo "  stop:        sudo systemctl stop $SERVICE"
echo "  boot-delay:  enabled (auto-starts on power-on/recovery)"