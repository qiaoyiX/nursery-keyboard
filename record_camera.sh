#!/usr/bin/env bash
# Record footage from the TAPO camera for offline tuning/training of the sleep monitor.
#
# Usage (on the Pi):
#   bash record_camera.sh [minutes]   # default 120 (2 hours)
#
# Reads camera_rtsp_url from settings.json. Saves 10-minute .mp4 segments (stream copy,
# no re-encoding — negligible CPU) into recordings/<timestamp>/. 2h of the TAPO C110
# sub-stream (640x360) is roughly 400-700 MB; check free space first.
#
# Needs ffmpeg:  sudo apt install -y ffmpeg
#
# Afterwards, analyze on any machine with:  python3 replay_sleep.py recordings/<ts>/*.mp4

set -euo pipefail
cd "$(dirname "$0")"

MINUTES="${1:-120}"

RTSP_URL=$(python3 -c "import json; print(json.load(open('settings.json')).get('camera_rtsp_url',''))")
if [ -z "$RTSP_URL" ]; then
    echo "ERROR: camera_rtsp_url is not set in settings.json" >&2
    exit 1
fi

if ! command -v ffmpeg >/dev/null; then
    echo "ERROR: ffmpeg not installed. Run: sudo apt install -y ffmpeg" >&2
    exit 1
fi

OUT_DIR="recordings/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUT_DIR"

echo "Recording ${MINUTES} minutes from camera into ${OUT_DIR}/ (10-min segments)…"
echo "Free space: $(df -h . | awk 'NR==2 {print $4}')"

# -c copy: save the H.264 stream as-is (no transcode, ~0% CPU).
# Segments survive a crash/power loss — only the current segment is at risk.
ffmpeg -hide_banner -loglevel warning \
    -rtsp_transport tcp -i "$RTSP_URL" \
    -t "$((MINUTES * 60))" \
    -c copy -an \
    -f segment -segment_time 600 -reset_timestamps 1 \
    "${OUT_DIR}/seg_%03d.mp4"

echo "Done. Segments:"
ls -lh "$OUT_DIR"
echo
echo "Copy to your dev machine with:"
echo "  scp -r $(whoami)@$(hostname).local:$(pwd)/${OUT_DIR} ."
