#!/usr/bin/env bash
# Continuous crib-camera recording into a rolling per-day buffer, for replaying the
# windows sleep_score flags. Stream copy (-c copy) — no re-encoding, so this costs a
# memcpy and leaves the 1 fps detection loop alone.
#
# Segments land in $CRIB_FOOTAGE_DIR/YYYY-MM-DD/crib_HHMMSS.mp4; crib_retention.py
# owns the pruning. Reads camera_rtsp_url from settings.json, same as record_camera.sh.
#
# Run under systemd (see install.sh), or by hand:  ./record_crib_rolling.sh
set -euo pipefail

cd "$(dirname "$0")"

OUT_ROOT="${CRIB_FOOTAGE_DIR:-$(pwd)/crib_footage}"
MIN_FREE_GB="${CRIB_FOOTAGE_MIN_FREE_GB:-4}"
SEGMENT_SECONDS="${CRIB_SEGMENT_SECONDS:-600}"

RTSP_URL=$(python3 -c "import json; print(json.load(open('settings.json')).get('camera_rtsp_url',''))")
if [ -z "$RTSP_URL" ]; then
    echo "ERROR: camera_rtsp_url is not set in settings.json" >&2
    exit 1
fi

# Refuse to start on a nearly-full card. The sleep monitor, the nanny pipeline and
# the JSON stores share this disk; a diagnostic buffer must never be what fills it.
FREE_GB=$(df -BG --output=avail . | tail -1 | tr -dc '0-9')
if [ "$FREE_GB" -lt "$MIN_FREE_GB" ]; then
    echo "ERROR: only ${FREE_GB}GB free, need ${MIN_FREE_GB}GB — not starting." >&2
    exit 1
fi

mkdir -p "$OUT_ROOT"
echo "Recording crib camera into ${OUT_ROOT}/<date>/ (${SEGMENT_SECONDS}s segments, stream copy)"

# strftime in the segment pattern gives per-day directories for free, but ffmpeg will
# not create them — so pre-create today's and tomorrow's, and let the systemd unit's
# restart handle the rollover after that.
mkdir -p "$OUT_ROOT/$(date +%F)" "$OUT_ROOT/$(date -d tomorrow +%F 2>/dev/null || date +%F)"

exec ffmpeg -nostdin -loglevel warning \
    -rtsp_transport tcp -i "$RTSP_URL" \
    -c copy -an \
    -f segment -segment_time "$SEGMENT_SECONDS" -reset_timestamps 1 \
    -strftime 1 "$OUT_ROOT/%Y-%m-%d/crib_%H%M%S.mp4"
