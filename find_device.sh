#!/bin/bash
# Run this to confirm the keypad is visible and ready.
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
import sys
try:
    import evdev
except ImportError:
    print("evdev not installed. Run install.sh first.")
    sys.exit(1)

found = False
for path in evdev.list_devices():
    try:
        dev = evdev.InputDevice(path)
        keys = set(dev.capabilities().get(evdev.ecodes.EV_KEY, []))
        is_sayo = "sayodevice" in dev.name.lower()
        has_space = evdev.ecodes.KEY_SPACE in keys
        no_alpha = evdev.ecodes.KEY_A not in keys

        if is_sayo and has_space and no_alpha:
            print(f"  READY: {path}  =>  {dev.name}")
            found = True
        elif is_sayo:
            print(f"  FOUND (not matched): {path}  =>  {dev.name}")
    except Exception:
        pass

if not found:
    sayo = [p for p in evdev.list_devices()
            if "sayodevice" in (evdev.InputDevice(p).name.lower() if True else "")]
    print("  No ready SayoDevice keypad found.")
    print("  Make sure the keypad is plugged into the Pi.")
PYEOF
