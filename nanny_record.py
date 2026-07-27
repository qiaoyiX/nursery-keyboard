"""
Nanny-cam recorder daemon: capture N TAPO RTSP streams during the care window
(default Mon-Fri 10:00-18:00) as hourly raw segments for nanny_analyze.py.

Runs as an always-on systemd service (nursery-nanny-record.service,
Restart=always) rather than a timer-started job: a timer fired at 10:00 dies
silently if the Pi reboots mid-day, while a daemon re-enters the window on
boot and just produces an odd-length segment with a correct wall-clock name.

Per camera, inside the window, one ffmpeg child:
    -c copy  (stream copy, ~0% CPU)   -an  (NO audio — deliberate: recording
    a nanny's audio without consent is wiretap territory; keep it video-only)
    -f segment -segment_time 3600 -segment_atclocktime 1  (cut on clock hours)
    -strftime 1  (segment filename = true wall-clock start, which
                  nanny_analyze relies on for offset→wall-clock conversion)

Children are supervised: a camera that drops (wifi, power) is respawned every
RESPAWN_DELAY seconds with a recomputed -t until the window ends. Spawning is
refused below MIN_FREE_BYTES so a stuck analyzer can never fill the SD card.

Config via environment (see nanny_common docstring). Unconfigured (no cameras)
is not an error: log and idle. NOTE: systemd reads EnvironmentFile only at
service start, so after editing /etc/nursery-tracker/nanny.env run
`sudo systemctl restart nursery-nanny-record`.
"""

import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta

from nanny_common import (
    RAW_DIR, ensure_dirs, load_cameras, load_days, load_window,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [nanny_record] %(message)s")

RESPAWN_DELAY   = 60          # seconds before restarting a dead ffmpeg
POLL_SECONDS    = 30          # supervision loop tick
MIN_FREE_BYTES  = 2 * 1024**3
UNCONFIGURED_RECHECK = 3600


def window_bounds(now, window, days):
    """(in_window, next_start, today_end) for the current moment."""
    start_t, end_t = window
    today_start = datetime.combine(now.date(), start_t)
    today_end   = datetime.combine(now.date(), end_t)
    if now.weekday() in days and today_start <= now < today_end:
        return True, None, today_end
    # Find the next window start (today if still ahead, else scan forward).
    probe = now.date() if (now < today_start) else now.date() + timedelta(days=1)
    for _ in range(8):
        if probe.weekday() in days:
            candidate = datetime.combine(probe, start_t)
            if candidate > now:
                return False, candidate, None
        probe += timedelta(days=1)
    return False, now + timedelta(hours=1), None   # unreachable with sane config


def spawn_ffmpeg(camera, url, seconds_left):
    out_dir = os.path.join(RAW_DIR, camera)
    os.makedirs(out_dir, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-rtsp_transport", "tcp", "-i", url,
        "-t", str(int(seconds_left)),
        "-c", "copy", "-an",
        "-f", "segment", "-segment_time", "3600",
        "-segment_atclocktime", "1", "-reset_timestamps", "1",
        "-strftime", "1",
        os.path.join(out_dir, "%Y%m%d_%H%M%S.mp4"),
    ]
    proc = subprocess.Popen(cmd)
    logging.info("[%s] recording started (pid %d, %ds left in window)",
                 camera, proc.pid, int(seconds_left))
    return proc


def record_window(cameras, window_end):
    """Supervise one ffmpeg per camera until window_end."""
    children = {}   # camera -> (proc, last_death_time)
    try:
        while True:
            now = datetime.now()
            left = (window_end - now).total_seconds()
            if left <= 0:
                break
            free = shutil.disk_usage(RAW_DIR).free
            for camera, url in cameras.items():
                proc, died_at = children.get(camera, (None, 0.0))
                if proc is not None and proc.poll() is None:
                    continue
                if proc is not None and died_at == 0.0:
                    # Just noticed the death; start the respawn clock.
                    logging.warning("[%s] ffmpeg exited (code %s) with %ds left — "
                                    "respawn in %ds", camera, proc.returncode,
                                    int(left), RESPAWN_DELAY)
                    children[camera] = (proc, time.time())
                    continue
                if proc is not None and time.time() - died_at < RESPAWN_DELAY:
                    continue
                if left <= RESPAWN_DELAY:
                    continue   # not worth restarting for the window's last minute
                if free < MIN_FREE_BYTES:
                    logging.error("[%s] NOT recording: only %.1f GB free (< 2 GB floor)",
                                  camera, free / 1024**3)
                    children[camera] = (proc, time.time())   # re-check after delay
                    continue
                children[camera] = (spawn_ffmpeg(camera, url, left), 0.0)
            time.sleep(POLL_SECONDS)
    finally:
        for camera, (proc, _) in children.items():
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                logging.info("[%s] recording stopped (window end)", camera)


def main():
    ensure_dirs()
    while True:
        try:
            cameras = load_cameras()
            window  = load_window()
            days    = load_days()
        except ValueError as e:
            logging.error("Bad configuration: %s — re-checking in %ds", e,
                          UNCONFIGURED_RECHECK)
            time.sleep(UNCONFIGURED_RECHECK)
            continue
        if not cameras:
            logging.info("No NANNY_CAM_* configured — idle, re-checking in %ds",
                         UNCONFIGURED_RECHECK)
            time.sleep(UNCONFIGURED_RECHECK)
            continue

        now = datetime.now()
        in_window, next_start, window_end = window_bounds(now, window, days)
        if not in_window:
            sleep_s = min((next_start - now).total_seconds(),
                          UNCONFIGURED_RECHECK)   # wake hourly to pick up env edits
            logging.info("Outside care window — next start %s (sleeping %ds)",
                         next_start, int(sleep_s))
            time.sleep(max(sleep_s, 1))
            continue

        logging.info("Entering care window until %s with %d camera(s): %s",
                     window_end, len(cameras), ", ".join(cameras))
        record_window(cameras, window_end)
        logging.info("Care window ended.")


if __name__ == "__main__":
    main()
