#!/bin/bash
# Run this to confirm the keypad is visible and shows KEY_F13 capability.
# Usage: bash find_device.sh
echo "Input devices on this system:"
echo "------------------------------"
for dev in /dev/input/event*; do
    name=$(cat /sys/class/input/$(basename "$dev")/device/name 2>/dev/null || echo "(unknown)")
    echo "$dev  =>  $name"
done

echo ""
echo "Checking for F13 key capability (indicates programmed SayoDevice)..."
if command -v python3 &>/dev/null; then
    python3 - <<'PYEOF'
try:
    import evdev
except ImportError:
    print("evdev not installed yet. Run install.sh first.")
    exit()

found = False
for path in evdev.list_devices():
    try:
        dev = evdev.InputDevice(path)
        keys = dev.capabilities().get(evdev.ecodes.EV_KEY, [])
        if evdev.ecodes.KEY_F13 in keys:
            print(f"  FOUND: {path}  =>  {dev.name}")
            found = True
    except Exception:
        pass

if not found:
    print("  No device with KEY_F13 found.")
    print("  Make sure the keypad is plugged in and programmed at SayoDevice.com.")
PYEOF
fi
