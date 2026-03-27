#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup.sh — One-shot installer for the Telegram Daily Personal Assistant
# Tested on Ubuntu 20.04 / 22.04 / Debian 11
# Run as your normal user (NOT root), the script uses sudo where needed.
# Usage:  bash setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$PROJECT_DIR/venv"
SERVICE_NAME="telegram-assistant"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CURRENT_USER="$(whoami)"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   Telegram Daily Assistant — Installer               ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 1. System dependencies ────────────────────────────────────────────────────
echo "→ Installing system packages…"
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv curl git 2>/dev/null

# ── 2. Python virtual environment ─────────────────────────────────────────────
echo "→ Creating Python virtual environment…"
python3 -m venv "$VENV"
source "$VENV/bin/activate"

# ── 3. Python dependencies ────────────────────────────────────────────────────
echo "→ Installing Python dependencies…"
pip install --upgrade pip -q
pip install -r "$PROJECT_DIR/requirements.txt" -q
echo "   ✅ Python packages installed."

# ── 4. .env file ──────────────────────────────────────────────────────────────
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo ""
    echo "→ Creating .env file from template…"
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo ""
    echo "┌─────────────────────────────────────────────────────┐"
    echo "│  ACTION REQUIRED: Fill in your .env file            │"
    echo "│                                                     │"
    echo "│  nano $PROJECT_DIR/.env    │"
    echo "│                                                     │"
    echo "│  You need:                                          │"
    echo "│  • TELEGRAM_BOT_TOKEN   (from @BotFather)           │"
    echo "│  • GOOGLE_CLIENT_ID     (Google Cloud Console)      │"
    echo "│  • GOOGLE_CLIENT_SECRET (Google Cloud Console)      │"
    echo "│  • ORS_API_KEY          (openrouteservice.org)      │"
    echo "└─────────────────────────────────────────────────────┘"
    echo ""
    read -rp "Press ENTER after you have filled in .env to continue…"
fi

# ── 5. Initialise database ────────────────────────────────────────────────────
echo "→ Initialising SQLite database…"
"$VENV/bin/python" -c "
from database.db import init_db
init_db()
print('   ✅ Database tables created.')
"

# ── 6. Install systemd service ────────────────────────────────────────────────
echo "→ Installing systemd service…"
# Replace placeholder username in service file
sed "s/YOUR_LINUX_USERNAME/$CURRENT_USER/g" \
    "$PROJECT_DIR/telegram-assistant.service" \
    | sudo tee "$SERVICE_FILE" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
echo "   ✅ Service installed and enabled."

# ── 7. Done ───────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   ✅  Setup complete!                                ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "Next steps:"
echo ""
echo "  1. Start the bot:"
echo "     sudo systemctl start $SERVICE_NAME"
echo ""
echo "  2. Check it's running:"
echo "     sudo systemctl status $SERVICE_NAME"
echo ""
echo "  3. Watch live logs:"
echo "     sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "  4. Open Telegram and send your bot /start"
echo ""
echo "  To stop:    sudo systemctl stop $SERVICE_NAME"
echo "  To restart: sudo systemctl restart $SERVICE_NAME"
echo ""
