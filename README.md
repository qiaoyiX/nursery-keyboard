# Nursery Tracker

A Raspberry Pi web app for logging diaper changes and feeds using a 4-key USB keypad. Press a button on the pad → the event is logged with a timestamp and appears instantly on a dashboard you open on your phone.

**Hardware:**
- CanaKit Raspberry Pi (4 or 5)
- BTXETUEL 4-key SayoDevice pad (USB)

---

## Step 1 — Program the keypad

Do this on any Mac/Windows/Linux computer with Chrome or Edge.

1. Go to **[SayoDevice.com](https://sayodevice.com)** in Chrome or Edge
2. Plug the keypad into your computer via USB
3. Click **Connect** and select the keypad from the browser prompt
4. Set each key:
   - Key 1 → `F13`
   - Key 2 → `F14`
   - Key 3 → `F15`
   - Key 4 → `F16`
5. Click **Save** — the config is stored on the device itself

The keypad is now plug-and-play. You only need to do this once.

---

## Step 2 — Flash the Raspberry Pi SD card

On your Mac:

1. Download **[Raspberry Pi Imager](https://www.raspberrypi.com/software/)**
2. Insert the microSD card
3. Open Imager and click the **gear icon** (Edit Settings) before writing:
   - Hostname: `raspberrypi`
   - Enable SSH → Use password authentication
   - Username: `pi` / Password: *(something you'll remember)*
   - Wi-Fi: your home network name and password
   - Timezone: set to your local timezone
4. Choose OS: **Raspberry Pi OS (64-bit)**
5. Choose Storage: your SD card
6. Click **Write**

---

## Step 3 — Boot the Pi and connect

1. Insert the SD card into the Pi and plug in power
2. Wait ~60 seconds for first boot (green LED will flicker)
3. On your Mac, open Terminal and SSH in:

```bash
ssh pi@raspberrypi.local
```

Enter your password when prompted. If `raspberrypi.local` doesn't resolve, check your router's device list for the Pi's IP address and use that instead (e.g. `ssh pi@192.168.1.42`).

---

## Step 4 — Install the tracker

From the SSH session on the Pi:

```bash
git clone https://github.com/qiaoyiX/nursery-keyboard.git
cd nursery-keyboard
bash install.sh
```

This installs Python dependencies, sets up a systemd service (auto-starts on boot), and adds your user to the `input` group.

---

## Step 5 — Set up keypad permissions

Plug the keypad into a Pi USB port, then run:

```bash
bash setup_udev.sh
```

This detects the keypad's USB vendor/product ID and writes a device-specific permission rule. It only gives access to this exact keypad — not all input devices.

Unplug and replug the keypad after it finishes.

---

## Step 6 — Verify everything works

```bash
# Confirm the keypad is detected by the app
bash find_device.sh

# Watch live logs — press a key on the pad and you should see "Logged: Wet" etc.
sudo journalctl -u nursery-tracker -f
```

Open the dashboard on your phone:

```
http://raspberrypi.local:5000
```

Bookmark it to your home screen for one-tap access.

---

## Key layout

| Key | Action |
|-----|--------|
| 1   | Wet    |
| 2   | Dirty  |
| 3   | Both   |
| 4   | Feed   |

The on-screen buttons also work as a backup if the keypad isn't nearby.

---

## Troubleshooting

**`ssh: Could not resolve hostname raspberrypi.local`**
Wait 2 more minutes, or find the Pi's IP in your router's device list and SSH to that directly.

**`find_device.sh` reports no keypad found**
Make sure the keypad was programmed at SayoDevice.com with keys set to F13/F14/F15/F16, then unplug and replug it.

**Keypad is plugged in but nothing logs**
```bash
sudo journalctl -u nursery-tracker -f
```
Check the error message. If it says permission denied, re-run `bash setup_udev.sh` and unplug/replug the keypad.

**Service won't start**
```bash
sudo systemctl status nursery-tracker
```

**Debug: see all input devices the app can see**

Set `NURSERY_DEBUG=1` in the service environment, then visit `http://raspberrypi.local:5000/devices`. Remove it when done.

```bash
# Add to service temporarily
sudo systemctl edit nursery-tracker
# Add under [Service]:
#   Environment=NURSERY_DEBUG=1
sudo systemctl restart nursery-tracker
```

---

## Useful commands

```bash
# Restart the tracker
sudo systemctl restart nursery-tracker

# Stop the tracker
sudo systemctl stop nursery-tracker

# View logs
sudo journalctl -u nursery-tracker -f

# Check service status
sudo systemctl status nursery-tracker
```
