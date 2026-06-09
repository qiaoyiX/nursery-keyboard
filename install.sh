#!/bin/bash
set -e

echo "==> Checking for Raspberry Pi OS..."
if ! grep -qi "raspberry" /etc/os-release 2>/dev/null && ! grep -qi "debian" /etc/os-release 2>/dev/null; then
    echo "WARNING: This script is designed for Raspberry Pi OS (Debian-based)."
fi

echo "==> Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3-pip python3-venv python3-full

echo "==> Creating Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
pip install --quiet flask evdev

echo "==> Adding udev rule so /dev/input/event* is group-readable..."
sudo tee /etc/udev/rules.d/99-nursery-keypad.rules > /dev/null <<'EOF'
# Allow members of the input group to read all input devices
KERNEL=="event*", SUBSYSTEM=="input", GROUP="input", MODE="0660"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger

echo "==> Making sure $USER is in the input group..."
sudo usermod -aG input "$USER"

echo "==> Installing systemd service..."
SERVICE_FILE=/etc/systemd/system/nursery-tracker.service
WORK_DIR="$(pwd)"
USER_NAME="$USER"
PYTHON_BIN="$WORK_DIR/venv/bin/python"

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Nursery Tracker
After=network.target

[Service]
User=$USER_NAME
# Gives the service access to the input group immediately,
# without needing the user to log out first.
SupplementaryGroups=input
WorkingDirectory=$WORK_DIR
ExecStart=$PYTHON_BIN app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable nursery-tracker
sudo systemctl restart nursery-tracker

echo ""
echo "==> Done!"
echo ""
echo "    Dashboard:  http://$(hostname).local:5000"
echo "    Debug:      http://$(hostname).local:5000/devices"
echo "    Logs:       sudo journalctl -u nursery-tracker -f"
echo ""
echo "Plug in the keypad, then check /devices to confirm it's detected."
