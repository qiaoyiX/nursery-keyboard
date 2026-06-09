#!/bin/bash
set -e

echo "==> Installing dependencies..."
sudo apt-get update -qq
sudo apt-get install -y python3-pip python3-venv

echo "==> Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
pip install flask evdev

echo "==> Adding $USER to input group (for keypad access)..."
sudo usermod -aG input "$USER"

echo "==> Installing systemd service..."
SERVICE_FILE=/etc/systemd/system/nursery-tracker.service
WORK_DIR="$(pwd)"
USER_NAME="$USER"

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Nursery Tracker
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$WORK_DIR
ExecStart=$WORK_DIR/venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable nursery-tracker
sudo systemctl restart nursery-tracker

echo ""
echo "==> Done! Tracker is running."
echo "    Open on your phone: http://$(hostname).local:5000"
echo ""
echo "NOTE: Log out and back in (or reboot) so the input group takes effect."
