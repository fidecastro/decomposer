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
## Look engine

`engine/` is a Rust binary that reads NV12, grades it on the GPU and republishes
it to a v4l2loopback node. One compute dispatch per frame: NV12 in, NV12 out,
with no CPU-side format conversion.

```bash
cd engine && cargo build --release

# Call mode: read the camera's own node directly
./target/release/decomposer-engine --look noir --input /dev/video0 --output /dev/video10

# Studio mode has no V4L2 node, so frames arrive as raw NV12 on stdin.
# Holding this pipe is also what keeps manual focus and white balance applied.
decomposer stream-nv12 --focus 150 --wb 3200 \
  | decomposer-engine --input - --output /dev/video10 --look noir

# Benchmark without an output, or pipe raw frames out for inspection
./target/release/decomposer-engine --output null --look chrome --frames 120
./target/release/decomposer-engine --output - --frames 1 | ffmpeg -f rawvideo ...
```

Looks: `none`, `process`, `chrome`, `fade`, `instant`, `mono`, `noir`, `tonal`,
`transfer`. `--strength` blends between the original and the graded frame.

### Virtual camera setup

```bash
sudo install -m 0644 packaging/v4l2loopback.conf /etc/modprobe.d/
sudo install -m 0644 packaging/v4l2loopback-load.conf /etc/modules-load.d/
sudo modprobe v4l2loopback
```

`exclusive_caps=1` matters: without it Chrome, Zoom and Discord list the device
and then fail to open it.

### Measured

On an RTX 4090, grading is free — the pipeline is camera-bound:

| | passthrough | with a look |
|---|---|---|
| 1080p | 28.1 fps | 28.1 fps (`noir`) |
| 4K | 23.1 fps | 23.9 fps (`chrome`) |

The GPU stage costs nothing measurable, so the ceiling is the C1's USB bandwidth
(4K NV12 at 30 fps is ~373 MB/s against a ~400 MB/s practical limit), not the looks.

End to end into `/dev/video10` at 1080p Call mode holds **~28 fps**, and the node
advertises itself to consumers as `NV12 1920x1080 @ 30fps`. Verified by reading the
loopback back: `mono` arrives with chroma at exactly 128, `instant` warm-shifted to
U 116.7 / V 138.5 — the grade reaches the application, not just the engine.

### Studio-mode throughput

The Studio pipeline runs at **~25 fps** against Call mode's ~28, with the camera
itself delivering ~30. The shortfall is *not* the engine or the pipe: fed from a
file, the engine's stdin path sustains **950 fps** with a look applied, and
removing the loopback write changes nothing (25.5 vs 25.4). It is producer-side
cost in the Python bridge.

Two things were tried and did not move it: reading fd 0 directly instead of
through Rust's `BufReader`, and widening the pipe to the kernel maximum. Making
`Frame.nv12()` return a memoryview instead of a `.tobytes()` copy gained about
0.5 fps. A threaded writer gained nothing measurable but is kept, because it
bounds latency — it drops the oldest frame rather than letting a stalled
consumer build an unbounded backlog of stale video.

The remaining ~15% is not yet explained. It is worth revisiting when the daemon
replaces this pipe, since the daemon can hand frames over shared memory and
avoid the copy entirely.
