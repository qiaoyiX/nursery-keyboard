"""
Crib-ROI picker + stream health check for a new/moved camera.

Two things in one run:

  1. Health check — opens the RTSP stream exactly like sleep_monitor.py does
     (FFMPEG backend, TCP transport), reads a handful of frames, and reports
     resolution, decode success rate, and effective frame timing. If the stream
     is broken you find out here, with hints, before touching settings.json.

  2. ROI picker — saves the last good frame and generates roi_picker.html
     (self-contained, image embedded), which it opens in your browser. Drag a
     rectangle over the crib; the page shows the exact "sleep_crib_roi" line to
     paste into settings.json. Works with opencv-python-headless — no GUI
     windows needed.

Usage (from any machine on the same LAN as the camera — your Mac is fine):

    python3 pick_roi.py "rtsp://user:pass@CAMERA_IP:554/stream2"
    python3 pick_roi.py recordings/20260722_120000/seg_000.mp4   # or a file
    python3 pick_roi.py frame.jpg                                # or a still

Remember: the ROI must EXCLUDE the TAPO OSD timestamp (top-left) — its
per-second digit changes read as constant motion.
"""

import argparse
import base64
import os
import sys
import time
import webbrowser

import cv2

FRAME_JPG = "roi_frame.jpg"
PICKER_HTML = "roi_picker.html"

HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Crib ROI picker</title>
<style>
  body { font-family: -apple-system, sans-serif; margin: 20px; background: #1a1a2e; color: #eee; }
  h1 { font-size: 1.2em; }
  #wrap { position: relative; display: inline-block; cursor: crosshair; }
  #frame { display: block; max-width: 95vw; user-select: none; -webkit-user-drag: none; }
  #box { position: absolute; border: 2px solid #ff5c8a; background: rgba(255,92,138,.15); pointer-events: none; display: none; }
  #out { margin-top: 14px; font-size: 1.05em; }
  code { background: #16213e; padding: 6px 10px; border-radius: 6px; display: inline-block; }
  button { margin-left: 10px; padding: 6px 14px; border-radius: 6px; border: none; background: #ff5c8a; color: #fff; cursor: pointer; }
  .hint { color: #9aa; font-size: .9em; margin-top: 10px; }
</style>
</head>
<body>
<h1>Drag a rectangle over the crib</h1>
<div id="wrap">
  <img id="frame" src="data:image/jpeg;base64,__IMG_B64__">
  <div id="box"></div>
</div>
<div id="out">Drag to select. Re-drag any time to redo.</div>
<p class="hint">Include the whole mattress area; EXCLUDE the camera's timestamp overlay
(top-left) and anything outside the crib that moves (curtains, doorway, mobile).</p>
<script>
const img = document.getElementById('frame');
const box = document.getElementById('box');
const out = document.getElementById('out');
const wrap = document.getElementById('wrap');
let start = null;

function pos(e) {
  const r = img.getBoundingClientRect();
  return [
    Math.min(Math.max(e.clientX - r.left, 0), r.width),
    Math.min(Math.max(e.clientY - r.top, 0), r.height),
  ];
}

wrap.addEventListener('mousedown', e => { start = pos(e); e.preventDefault(); });

window.addEventListener('mousemove', e => {
  if (!start) return;
  draw(start, pos(e));
});

window.addEventListener('mouseup', e => {
  if (!start) return;
  const end = pos(e);
  draw(start, end);
  report(start, end);
  start = null;
});

function draw(a, b) {
  box.style.display = 'block';
  box.style.left   = Math.min(a[0], b[0]) + 'px';
  box.style.top    = Math.min(a[1], b[1]) + 'px';
  box.style.width  = Math.abs(b[0] - a[0]) + 'px';
  box.style.height = Math.abs(b[1] - a[1]) + 'px';
}

function report(a, b) {
  const r = img.getBoundingClientRect();
  const f = v => Math.round(v * 1000) / 1000;
  const x0 = f(Math.min(a[0], b[0]) / r.width);
  const y0 = f(Math.min(a[1], b[1]) / r.height);
  const x1 = f(Math.max(a[0], b[0]) / r.width);
  const y1 = f(Math.max(a[1], b[1]) / r.height);
  if (x1 - x0 < 0.02 || y1 - y0 < 0.02) { out.textContent = 'Rectangle too small — drag again.'; return; }
  const line = `"sleep_crib_roi": [${x0}, ${y0}, ${x1}, ${y1}],`;
  out.textContent = '';
  const code = document.createElement('code');
  code.textContent = line;
  const btn = document.createElement('button');
  btn.textContent = 'Copy';
  btn.onclick = () => {
    navigator.clipboard.writeText(line);
    btn.textContent = 'Copied!';
  };
  out.append(code, btn);
}
</script>
</body>
</html>
"""


def open_capture(url):
    # Same transport setup as sleep_monitor.open_capture (not imported to keep
    # this runnable on machines without the monitor's other dependencies).
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def health_check(url, n_frames):
    """Read n_frames, print timing/resolution stats. Returns last good frame or None."""
    print(f"Opening: {url}")
    t0 = time.time()
    cap = open_capture(url)
    if not cap.isOpened():
        print("!! Could not open the stream at all.")
        print_hints(url)
        return None
    ok, frame = cap.read()
    if not ok or frame is None:
        print("!! Stream opened but no frames decode.")
        print_hints(url)
        cap.release()
        return None

    h, w = frame.shape[:2]
    print(f"Stream opened in {time.time() - t0:.1f}s — {w}x{h}")

    good, last = 1, frame
    t_read = time.time()
    for i in range(n_frames - 1):
        ok, frame = cap.read()
        if ok and frame is not None:
            good += 1
            last = frame
    elapsed = time.time() - t_read
    cap.release()

    fps = (good - 1) / elapsed if elapsed > 0 else 0
    print(f"Decoded {good}/{n_frames} frames in {elapsed:.1f}s (~{fps:.1f} fps)")
    if good < n_frames:
        print("!! Some frames failed to decode — weak wifi or camera still settling."
              " Re-run to see if it persists.")
    else:
        print("Stream looks healthy.")
    return last


def print_hints(url):
    print()
    print("Common causes for TAPO cameras:")
    print("  - Username/password must be the CAMERA ACCOUNT (app: camera Settings ->")
    print("    Advanced -> Camera Account), NOT your TP-Link cloud login.")
    print("  - Special characters in the password must be URL-encoded (@ -> %40 etc).")
    print("  - Path is /stream1 (main) or /stream2 (substream) — try the other one.")
    print("  - Wrong IP: check the camera's current IP in the app or your router,")
    print("    and give it a DHCP reservation so it stops moving.")
    print(f"  - Quick independent test in VLC: File -> Open Network -> {url}")


def write_picker(frame):
    cv2.imwrite(FRAME_JPG, frame)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        print("!! Could not encode frame for the picker.")
        return
    html = HTML_TEMPLATE.replace("__IMG_B64__", base64.b64encode(buf).decode("ascii"))
    with open(PICKER_HTML, "w") as f:
        f.write(html)
    print(f"Frame saved: {FRAME_JPG}")
    print(f"Picker:      {PICKER_HTML} — opening in browser...")
    webbrowser.open("file://" + os.path.abspath(PICKER_HTML))
    print()
    print("Drag a rectangle over the crib, click Copy, and paste the line into")
    print("settings.json on the Pi. Then restart nursery-sleep-monitor and press")
    print('"Crib is empty" on the dashboard with the crib actually empty.')


def main():
    ap = argparse.ArgumentParser(description="Check a camera stream and pick the crib ROI.")
    ap.add_argument("source", help="rtsp:// URL, or a video/image file")
    ap.add_argument("--frames", type=int, default=10, help="frames to read for the health check")
    args = ap.parse_args()

    if args.source.lower().endswith((".jpg", ".jpeg", ".png")):
        frame = cv2.imread(args.source)
        if frame is None:
            print(f"!! Could not read image {args.source}")
            sys.exit(1)
    else:
        frame = health_check(args.source, args.frames)
        if frame is None:
            sys.exit(1)

    print()
    write_picker(frame)


if __name__ == "__main__":
    main()
