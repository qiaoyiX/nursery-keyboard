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
pip install --quiet -r requirements.txt

echo "==> Making sure $USER is in the input group..."
sudo usermod -aG input "$USER"
echo "    NOTE: run 'bash setup_udev.sh' once with the keypad plugged in"
echo "          to create a device-specific udev rule (recommended)."

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

echo "==> Installing sleep monitor service..."
SLEEP_SERVICE_FILE=/etc/systemd/system/nursery-sleep-monitor.service
sudo tee "$SLEEP_SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Nursery Sleep Monitor
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$WORK_DIR
ExecStart=$PYTHON_BIN sleep_monitor.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable nursery-sleep-monitor

echo "==> Installing Neon backup sync timer (runs only if configured)..."
BACKUP_ENV_FILE=/etc/nursery-tracker/backup.env
sudo mkdir -p /etc/nursery-tracker
if [ ! -f "$BACKUP_ENV_FILE" ]; then
    sudo tee "$BACKUP_ENV_FILE" > /dev/null <<EOF
# Neon backup target for backup_sync.py (leave unset to disable off-site backup).
# DATABASE_URL=postgresql://user:pass@host/db?sslmode=require
EOF
    sudo chmod 600 "$BACKUP_ENV_FILE"
fi

sudo tee /etc/systemd/system/nursery-backup.service > /dev/null <<EOF
[Unit]
Description=Nursery Tracker - snapshot local JSON store to Neon backup
# Skips (not fails) when no backup target is configured
ConditionPathExists=$BACKUP_ENV_FILE

[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$WORK_DIR
EnvironmentFile=$BACKUP_ENV_FILE
ExecStart=$PYTHON_BIN backup_sync.py
EOF

sudo tee /etc/systemd/system/nursery-backup.timer > /dev/null <<EOF
[Unit]
Description=Nursery Tracker backup sync every 6 hours

[Timer]
OnCalendar=00/6:20
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now nursery-backup.timer

echo ""
echo "==> Done!"
echo ""
echo "    Dashboard:  http://$(hostname).local:8080"
echo "    Logs:       sudo journalctl -u nursery-tracker -f"
echo "    Sleep logs: sudo journalctl -u nursery-sleep-monitor -f"
echo ""
echo "Next steps:"
echo "  1. Plug in the keypad"
echo "  2. Run: bash setup_udev.sh   (creates a scoped device rule)"
echo "  3. Run: bash find_device.sh  (confirms the keypad is detected)"
echo "  4. In Tapo app: Settings → Advanced Settings → Camera Account → create credentials"
echo "  5. Edit settings.json: set camera_rtsp_url to rtsp://user:pass@IP:554/stream2"
echo "  6. Run: sudo systemctl start nursery-sleep-monitor"
