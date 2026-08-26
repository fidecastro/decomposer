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

### Camera control

```bash
# What the device actually is
decomposer camera-info

# Manual focus, white balance, exposure. -1 returns a control to auto.
decomposer control --focus 150 --wb 3200 --iso 400 --exposure 12000
decomposer control --auto

# Map the vendor UVC Extension Unit (read-only diagnostic)
decomposer probe-xu
```

**While any of these run, `/dev/video0` disappears.** Attaching XLink tears down
the camera's UVC interfaces; they return about 14 seconds after the command
exits. This is a property of the hardware, not a bug — see `docs/camera-notes.md`.

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
