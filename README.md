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
- [ ] Mac reference stills (`references/`)
- [ ] Linux raw UVC preview
- [ ] Host look engine (Process / Noir / Chrome first)
- [ ] `v4l2loopback` virtual cam

## Quick start (Linux)

```bash
cd decomposer
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# List cameras
decomposer devices

# Preview with a look
decomposer preview --look noir

# Publish to v4l2loopback (after: sudo modprobe v4l2loopback)
decomposer virtual --look process --out /dev/video10
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
