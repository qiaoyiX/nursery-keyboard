#!/bin/bash
# Run this to confirm the keypad is visible and shows KEY_F13 capability.
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
echo "Checking for F13 key capability (indicates programmed SayoDevice)..."

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

full_kb = {evdev.ecodes.KEY_A, evdev.ecodes.KEY_Z, evdev.ecodes.KEY_SPACE}
found = False
sayo_devices = []

for path in evdev.list_devices():
    try:
        dev = evdev.InputDevice(path)
        keys = set(dev.capabilities().get(evdev.ecodes.EV_KEY, []))
        is_sayo = "sayodevice" in dev.name.lower()
        has_f13 = evdev.ecodes.KEY_F13 in keys
        is_full_kb = not keys.isdisjoint(full_kb)

        if is_sayo:
            sayo_devices.append((path, dev.name, has_f13))

        if has_f13 and not is_full_kb:
            print(f"  READY: {path}  =>  {dev.name}")
            found = True
    except Exception:
        pass

if not found:
    if sayo_devices:
        print("  SayoDevice is plugged in but NOT yet programmed to F13-F16:")
        for path, name, has_f13 in sayo_devices:
            print(f"    {path}  =>  {name}")
        print()
        print("  Fix: unplug the keypad, plug into your Mac/PC, open")
        print("  SayoDevice.com in Chrome, and set keys to F13/F14/F15/F16.")
    else:
        print("  No SayoDevice found. Make sure the keypad is plugged in.")
PYEOF
