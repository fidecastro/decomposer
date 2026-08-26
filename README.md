# decomposer

Linux-first (Windows later) toolkit for the **Opal C1**: capture the UVC stream and apply **Composer-inspired looks** on the host, then optionally publish a virtual webcam.

This is **not** Opal Composer. It does not redistribute Opal binaries, firmware, or models. Looks are clean-room approximations of Composer’s named photo effects (and later custom looks), tuned against Mac reference stills.

## Camera (known-good on this project)

| | |
|---|---|
| Device | Opal C1 |
| Firmware | 4.10 |
| USB | `03E7:F63D` (Intel Movidius / Luxonis family VID) |
| Serial | `1844301061E55F1700` |
| Mac reference app | Opal Composer **1.4.4** |

## Status

- [x] Project scaffold
- [x] Linux capture confirmed — stock `uvcvideo`, NV12 4K30 / 1080p30, no vendor software
- [x] Extension Unit mapped (`decomposer probe-xu`) — see `docs/camera-notes.md`
- [x] XLink/DepthAI control verified — manual focus, white balance, exposure/ISO all exact
- [ ] XLink capture -> look engine -> `v4l2loopback`
- [ ] Mac reference stills (`references/`)
- [ ] Host look engine (Process / Noir / Chrome first)
- [ ] `v4l2loopback` virtual cam

## Quick start (Linux)

```bash
cd decomposer
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### One-time setup

The camera's manual controls live on XLink, which is reached through libusb, so
the USB node needs an ACL:

```bash
sudo install -m 0644 packaging/60-opal-c1.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add --subsystem-match=usb
```

Without it `depthai` reports zero devices and logs "Insufficient permissions".

### Two modes

The C1 runs one of two firmwares and cannot run both, so decomposer makes the
tradeoff explicit rather than hiding it:

| | **Call mode** (`f63d`) | **Studio mode** (`f63b`) |
|---|---|---|
| Microphone | **yes** | no |
| `/dev/video0` | **yes** | no |
| Manual focus | no | **yes** |
| Manual white balance | no | **yes** |
| Manual exposure + ISO | yes | yes |
| Brightness / contrast / saturation / hue / sharpness | yes | yes |
| Host-side looks | yes | yes |

Call mode is the default and covers ordinary use: the mic works, any app can
open the camera, and everything except focus and white balance is adjustable
with no interruption. Studio mode buys those two controls by rebooting the
camera into stock DepthAI firmware, which has no UVC and no audio.

Switching costs about 5 s into Studio and ~15 s back, because the device
re-enumerates each way. Studio settings live only while the connection is held.

```bash
# Which mode am I in, and what does each offer?
decomposer mode

# Call mode - instant, no interruption, mic stays up
decomposer control                                  # show current values
decomposer control --brightness 140 --saturation 62
decomposer control --exposure 12000 --iso 400       # engages Manual Mode first
decomposer control --auto                           # hand exposure back to the camera

# Studio mode - explicit, because it costs the mic
decomposer control --studio --focus 150 --wb 3200 --hold 10

# Device facts, and the read-only extension-unit diagnostic
decomposer camera-info
decomposer probe-xu
```

Asking for `--focus` or `--wb` without `--studio` is refused with an explanation
rather than silently taking your microphone away.

### Looks (work in progress)

```bash
decomposer preview --look noir
decomposer virtual --look process --out /dev/video10   # needs v4l2loopback
```

## Mac reference capture

With Composer 1.4.4 preview working:

```bash
# Raw C1 (quit Composer first so it releases the device)
decomposer capture-ref --source "Opal C1" --out references/raw-c1.png

# Composer virtual cam (Composer must be open with the look selected)
decomposer capture-ref --source "Opal Composer" --out references/composer-default.png
```

## Layout

```
decomposer/
  README.md
  docs/camera-notes.md
  references/           # Mac / Linux stills for A/B
  src/opal_c1/          # Python package
  scripts/              # helpers
  requirements.txt
  pyproject.toml
```

## Non-goals (for now)

- Composer UI / stickers / decorations
- Firmware flashing
- Shipping Opal’s DepthAI dylib or CoreML models
