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

echo "==> Installing nanny report services (run only if configured)..."
sudo apt-get install -y ffmpeg
NANNY_ENV_FILE=/etc/nursery-tracker/nanny.env
if [ ! -f "$NANNY_ENV_FILE" ]; then
    sudo tee "$NANNY_ENV_FILE" > /dev/null <<EOF
# Nanny report configuration (leave unset to disable the feature).
# GEMINI_API_KEY=...
# GEMINI_MODEL=gemini-2.5-flash-lite
# One line per camera, name=rtsp-url (name becomes a directory: letters/digits/_/- only).
# Use each camera's Camera Account credentials and the low-res sub-stream (stream2).
# NANNY_CAM_1=nurserycam=rtsp://user:pass@192.168.1.x:554/stream2
# NANNY_CAM_2=playcam=rtsp://user:pass@192.168.1.y:554/stream2
# NANNY_CAM_3=bedcam=rtsp://user:pass@192.168.1.z:554/stream2
# Which room each camera watches. Cameras sharing a room are treated as two
# angles on one scene: that is how "the baby is awake in this room" from one
# camera makes the other camera's phone use count as unauthorized, and how a
# caregiver alone in another room is NOT read as the baby being left alone.
# NANNY_CAM_ROOMS=nurserycam:nursery,playcam:nursery,bedcam:bedroom
# NANNY_WINDOW=10:00-18:00
# NANNY_DAYS=Mon,Tue,Wed,Thu,Fri
# NANNY_CLIP_RETENTION_DAYS=14
EOF
    sudo chmod 600 "$NANNY_ENV_FILE"
fi

sudo tee /etc/systemd/system/nursery-nanny-record.service > /dev/null <<EOF
[Unit]
Description=Nursery Nanny cam recorder (care window enforced internally)
After=network-online.target
# Skips (not fails) when the feature is not configured
ConditionPathExists=$NANNY_ENV_FILE

[Service]
User=$USER_NAME
WorkingDirectory=$WORK_DIR
EnvironmentFile=$NANNY_ENV_FILE
ExecStart=$PYTHON_BIN nanny_record.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/nursery-nanny-analyze.service > /dev/null <<EOF
[Unit]
Description=Nursery Nanny footage analyzer (Gemini)
ConditionPathExists=$NANNY_ENV_FILE

[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$WORK_DIR
EnvironmentFile=$NANNY_ENV_FILE
ExecStart=$PYTHON_BIN nanny_analyze.py
EOF

sudo tee /etc/systemd/system/nursery-nanny-analyze.timer > /dev/null <<EOF
[Unit]
Description=Nursery Nanny hourly footage analysis

[Timer]
OnCalendar=*-*-* 11..18:05:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo tee /etc/systemd/system/nursery-nanny-report.service > /dev/null <<EOF
[Unit]
Description=Nursery Nanny daily report merge
ConditionPathExists=$NANNY_ENV_FILE

[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$WORK_DIR
EnvironmentFile=$NANNY_ENV_FILE
ExecStart=$PYTHON_BIN nanny_report.py
EOF

sudo tee /etc/systemd/system/nursery-nanny-report.timer > /dev/null <<EOF
[Unit]
Description=Nursery Nanny daily report at 18:45

[Timer]
OnCalendar=*-*-* 18:45:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now nursery-nanny-record.service
sudo systemctl enable --now nursery-nanny-analyze.timer
sudo systemctl enable --now nursery-nanny-report.timer

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
echo "  7. Nanny report (optional): create Camera Accounts on the nanny cams, then"
echo "     sudo nano /etc/nursery-tracker/nanny.env  (uncomment + fill in keys/cameras)"
echo "     sudo systemctl restart nursery-nanny-record"
