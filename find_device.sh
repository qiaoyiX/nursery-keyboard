#!/bin/bash
# Run this to confirm the keypad is visible and verify key mappings.
# Usage: bash find_device.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"

echo "Input devices on this system:"
echo "------------------------------"
for dev in /dev/input/event*; do
    name=$(cat /sys/class/input/$(basename "$dev")/device/name 2>/dev/null || echo "(unknown)")
    echo "$dev  =>  $name"
done

echo ""
echo "Checking for SayoDevice keypad..."

if [ ! -x "$VENV_PYTHON" ]; then
    echo "evdev not installed yet. Run install.sh first."
    exit 1
fi

"$VENV_PYTHON" - <<'PYEOF'
import sys, select, time
try:
    import evdev
except ImportError:
    print("evdev not installed. Run install.sh first.")
    sys.exit(1)

found = None
for path in evdev.list_devices():
    try:
        dev = evdev.InputDevice(path)
        name = dev.name.lower()
        if "sayodevice" in name and "keyboard" in name:
            found = dev
            print(f"  READY: {dev.path}  =>  {dev.name}")
            break
    except Exception:
        pass

if not found:
    # Show any SayoDevice interfaces that were seen
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            if "sayodevice" in dev.name.lower():
                print(f"  FOUND (no keyboard interface): {dev.path}  =>  {dev.name}")
        except Exception:
            pass
    print("\n  No SayoDevice keyboard interface found.")
    print("  Make sure the keypad is plugged into the Pi.")
    sys.exit(1)

print("\n  Press each key now to verify mappings (listening 8 seconds)...")
deadline = time.time() + 8
while time.time() < deadline:
    r, _, _ = select.select([found.fd], [], [], deadline - time.time())
    if r:
        for event in found.read():
            if event.type == evdev.ecodes.EV_KEY and event.value == 1:
                kname = evdev.ecodes.KEY.get(event.code, str(event.code))
                print(f"    key pressed: {kname}")
PYEOF
