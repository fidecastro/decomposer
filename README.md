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

Looks: `none`, then Composer's eight Core Image effects — `process`, `chrome`,
`fade`, `instant`, `mono`, `noir`, `tonal`, `transfer` — and its own five,
`G1`, `D1`, `Q1`, `S1`, `X1`. `--strength` blends between the original and the
graded frame.

**These are not approximations.** Each one is a 3D LUT measured from Composer
itself; see *Looks, measured* below.

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

## Architecture

Hexagonal, with the walls enforced by tests rather than convention:

```
src/opal_c1/
  core/         pure decisions: model, transitions, health policy, presets.
                No IO of any kind - a clock or a socket here fails the suite.
  ports.py      what the application may know about a camera or the engine
  adapters/     where ports meet the machine: UVC, depthai, engine process
  daemon.py     the application: one transition worker, supervisor, poller,
                stall watchdog, JSON-RPC over a unix socket
  gui.py, cli.py  clients of the daemon, never owners of hardware
engine/         Rust: capture -> GPU look -> v4l2loopback, one Config struct
                fed by both argv and the control socket
tests/          the camera's misbehavior catalogue, replayed in milliseconds
```

Three properties the tests pin: the core imports no IO; dependencies point
inward only (core ← ports ← adapters ← app ← ui); and the engine protocol is
composed in exactly one module, so command-line and runtime configuration
cannot drift apart again. Direct-hardware CLI commands refuse to run while a
daemon owns the camera - two owners was a real failure mode, not a
hypothetical.

## Daemon

The daemon owns the camera and the look engine, and everything else is a client
of it. That is not architecture for its own sake: Studio-mode settings exist only
while the XLink connection is held, so whoever holds the device *is* the
settings, and restarting the engine casually would pull `/dev/video10` out from
under whatever application is using the camera.

```bash
decomposer daemon              # holds the camera, publishes to /dev/video10
decomposer status              # what it is doing, plus the engine's own output
decomposer look noir           # applies on the next frame - no restart, no flicker
decomposer switch studio       # ~5s; ~15s coming back
decomposer set --brightness 150 --iso 400
decomposer set --focus 150     # Studio mode only, live
decomposer stop
```

Look changes go over a control socket to the running engine, so the virtual
camera never drops. Mode switches do restart the engine, since the input source
changes — but a consumer attached to `/dev/video10` survives it.

If the engine dies, the daemon restarts it. This is not hypothetical: the camera
re-enumerated under a running engine during testing and it exited with `ENODEV`.
The engine's stderr is drained into a ring buffer and shown in `status`, so a
failure explains itself instead of vanishing.

## Overlay

```bash
decomposer toggle      # show, or hide if already up
```

Not a settings window. It is a Wayland layer surface anchored to the top right,
sized like a camera's on-screen display: live preview first, then small controls.
Running `toggle` again hides it, so one bar entry or keybind serves as on/off.
Escape closes it.

GTK4 and libadwaita, themed from the active Omarchy palette at
`~/.local/state/omarchy/current/theme/colors.toml` and using the desktop UI font.
Switching Omarchy themes recolours it live.

The preview comes from the **engine**, not the camera: the panel must never open
the camera itself, because in Studio mode there is no V4L2 node and in Call mode
a second reader competes with the engine. The engine downscales a frame it
already has to 480x270 RGB and serves it on a Unix socket, publishing every
other frame and dropping frames for a slow panel rather than stalling the
pipeline.

Every slider's number is an **editable value box**: click it, type an exact
ISO or exposure, Enter commits (clamped to range). While you are touching a
slider the status poller keeps its hands off it for a few seconds, so adjusting
a control the camera is auto-driving is no longer a tug-of-war — the readback
resumes flowing once you let go.

When no frames arrive the preview shows a placeholder instead of freezing on
the last frame: a NO FEED card, or classic broadcast colour bars.
**Right-click the preview** to switch between them; the choice persists in
`~/.config/decomposer/panel.json`.

**Background blur and replacement**: `decomposer blur 0.6` masks you out
with person segmentation (the bundled MediaPipe model, Apache-2.0, running
on CPU in single-digit milliseconds) and disc-blurs everything else on the
GPU; `decomposer background ~/beach.png` composites an image behind you
instead. Both live in the panel as a Blur slider and a Backdrop chooser,
and both work identically in Call and Studio mode.

**The model chain**: run the feed through any ONNX models of your own —
`decomposer model add style.onnx --device cuda`, then
`decomposer model strength 0 0.5` (live, no restart). A model with a
one-channel output joins the person mask (strength = its weight); a
three-channel output recolors the frame (strength = blend), applied as a
detail-preserving residual so a low-resolution model never softens the
image. The panel's Models section gives each entry a strength slider, a
cpu/cuda toggle, and a remove button.

The mask is a port, not a feature: bring your own model with
`decomposer daemon --seg-model your.onnx` (any image-in/mask-out ONNX;
shapes are autodetected) and pick where it runs with `--seg-device cpu|cuda`.
Or bypass ONNX entirely — connect to the engine's `mask.sock`, send
`u32 width, u32 height` (LE) and then raw `w*h` u8 mask frames from any
process in any framework; the internal model yields while you're connected.
See docs/background-blur.md.

**Health check**: `decomposer doctor` walks the whole stack — engine,
LUTs, model, loopback module, udev rules, USB quirk, camera presence,
layer-shell, daemon — and says exactly which piece is missing and how to
fix it. `decomposer install-service` writes a systemd user unit
(`systemctl --user enable --now decomposer`), and `packaging/PKGBUILD` is
a starting point for an Arch package.

A **MIC chip** in the mode row tells the audio truth: green when the C1's
microphone card is actually registered (Call mode), dimmed when it does not
exist (Studio firmware has no audio at all — a hardware fact, not a setting).
If the C1 is your system default microphone, switching to Studio flashes a
heads-up that apps will fall back to another source.

Focus and white balance each have an **auto** button. When a control is on
automatic the button lights and the value reads `auto` — the daemon stores auto
as `-1`, and clamping that onto the slider would display `0`, a real and very
different setting.

### Looks, measured

The looks began as hand-tuned curves guessing at Apple's `CIPhotoEffect`
pipelines, and Composer's own five (`G1 D1 Q1 S1 X1`) were Metal shaders we
could not read at all. They are now measured instead.

`references/color-target.png` walks the whole RGB cube — 4096 patches, plus a
skin-tone strip. Rendering it through Composer's own shaders gives an exact
input/output pair for every colour. Each look was then checked for a spatial
component (does the output depend on *where* a pixel is, as it would with a
vignette or grain?) by grouping pixels by input colour and measuring the spread
of their outputs. **All thirteen came back with a spread of exactly zero**, which
means each is a pure colour transform and a LUT reproduces it precisely rather
than approximately.

`scripts/fit_luts.py` extracts a 16³ `.cube` per look — measured at every one of
its own grid points, so nothing is interpolated at extraction time. Replaying
each LUT onto the baseline reproduces Composer's render with **zero error**.

End to end through the NV12 pipeline, against Composer's own output:

| | median | p95 | max |
|---|---|---|---|
| error per channel, 0–255 | 0.3–0.5 | ≤1.7 | ≤6 |

That residual is the 8-bit YCbCr round trip, not the LUT. The previous hand-tuned
curves scored a median of 18–46 with a maximum near 198.

The engine prefers `luts/<name>.cube` when it exists and falls back to the
built-in curves when it does not, so any `.cube` file works — the look engine is
open-ended rather than a fixed list.

#### Intensity

The LUTs are each filter measured **at full strength**, because that is what the
shaders do when handed an image. `--strength` blends linearly between the input
and the filtered result, which is the same operation MetalPetal's `intensity`
performs — verified against the references: at `0.0` the output reproduces the
source, at `0.5` it lands on the exact midpoint, at `1.0` on the full look, all
within the pipeline's own ~1.1/255 round-trip floor.

Looks therefore start at **0.5**. Full strength is the filter as its shader
defines it, which is stronger than these are usually wanted; half is a better
starting point and each look then remembers whatever you dial in.
`--default-strength` changes where new looks begin.

Intensity is remembered **per look**, since Composer's filters carry their own:
a strength dialled in for `noir` does not follow you to `G1`.

### Presets

```bash
decomposer preset save desk
decomposer preset list
decomposer preset load desk
decomposer preset delete desk
```

A preset captures the look and its intensity, mirroring, the overlay with its
placement and opacity, and the camera controls. They live in
`~/.config/decomposer/presets`.

The mode is recorded but **not** switched into on load unless `--with-mode` is
given: switching reboots the camera and takes about fifteen seconds, which is not
something a preset should do to you by surprise. Anything the current mode cannot
apply — a focus value while in Call mode, an overlay file that has since moved —
is reported rather than silently dropped.

The panel has a dropdown to load and a popover to save. A popover rather than a
dialog, because a layer surface cannot parent a dialog but `xdg_popup` is part of
the protocol and works.

### Resolution

```bash
decomposer resolution 1080p --capture-4k   # publish 1080p, capture 4K
decomposer resolution 4k
```

Choices: 720p, 1080p, 1440p, 4k, with `--capture-4k` to capture larger than
you publish (lossless zoom to the ratio). Applying restarts the engine — and in
Studio the camera session with it — and v4l2loopback keeps its old format while
any consumer holds the node, so attached applications must reconnect to see the
new size. The panel shows the selector in the header next to the mode pill.

### Clarity (CLAHE)

```bash
decomposer clahe 0.6
decomposer clahe off
```

Contrast Limited Adaptive Histogram Equalization on the GPU: three dispatches —
per-tile luma histograms (8×8 grid over the *visible* view, so a zoomed crop
gets its own contrast), clip-limited CDFs, then bilinear interpolation between
the four surrounding tiles' curves. Verified on synthetic frames: strength 0 is
exact identity, full strength expands a low-contrast texture 3.1×, and a
two-region frame has both regions stretched independently while their means
stay apart — local, not global. Costs ~5% GPU, invisible at camera rates.
Composer shipped CLAHE in its VideoService; this closes that gap. Part of
presets; the panel exposes it as the Clarity slider.

### Digital zoom

```bash
decomposer zoom 2 --x 0.3 --y -0.1    # 2x, window right-of-centre and up
decomposer zoom off
```

Zoom crops and scales in the shader — the read side is a bilinear sampler, so
mirroring, zooming and capture-to-output scaling are all one coordinate
mapping. On the panel: a slider, scroll-wheel over the preview to zoom, drag to
pan. Zoom and pan are part of presets.

By default the capture equals the output and zoom upscales. Run the daemon with
`--in-width 3840 --in-height 2160` and it captures 4K while publishing 1080p:
zoom is then **lossless to 2x** — verified against a synthetic frame, the 2x
output matches the exact centre crop with zero error — at the cost of a few fps
(the camera delivers 4K at ~22-27 rather than 30).

### Overlays

Composer called these stickers. A PNG is composited over the frame — a logo, a
watermark, a lower third:

```bash
decomposer overlay ~/logo.png --x 1480 --y 700 --width 380 --height 380 --opacity 0.9
decomposer overlay off
```

Position is in output pixels; width and height are maximums the image is fitted
into, keeping aspect ratio, with 0 meaning unconstrained. The panel has a file
picker, a clear button and an opacity slider.

The host decodes and rescales once, so the GPU gets a buffer already at final
size and the shader only does a rectangle test and a blend — no resampling per
frame. Downscaling uses a box filter with alpha-weighted colour, because nearest
sampling destroys a shrunken logo (thin strokes vanish) and unweighted averaging
drags dark fringes out of transparent pixels.

Compositing happens in RGB **before** the conversion back to YCbCr, so the
overlay is graded-*over* rather than graded — a logo keeps its own colours
whatever look is applied — and its alpha edge lands in RGB instead of being
smeared by chroma subsampling. Overlay coordinates are output-space, so
mirroring the image does not drag the logo along with it.

### Orientation

The C1's sensor is mounted upside down. Opal's own firmware corrects for it;
stock DepthAI firmware does not, so Studio mode used to deliver an image rotated
180° from Call mode. Measured rather than assumed — correlating the two modes
scored **+0.80 for rotate-180** and **−0.66 for identical** — and fixed on the
device with `setImageOrientation(ROTATE_180_DEG)`, so both modes now share one
reference.

On top of that, mirroring is a user preference applied in the shader, where it
costs nothing (it only changes where the shader reads):

```bash
decomposer mirror --horizontal on     # selfie view
decomposer mirror --vertical on       # both axes together is a 180° turn
```

Because both modes share an orientation, one mirror setting is meaningful across
them. 90° rotation is *not* supported: it swaps the output dimensions, which
means recreating `/dev/video10` at 1080x1920 and forcing every connected
application to renegotiate — a different and much more disruptive operation than
a free shader flip.

### The mark

Opal Composer's logo is a circle and a triangle in black and white, where the
overlap flips colour. `src/opal_c1/logo.py` borrows that idea in Omarchy's pixel
vocabulary: a pixelated semicircle for the D with a triangle driven into it, and
the intersection punched out rather than filled.

```
  ######      ##
  ########    ####
  ##########  ######
  ####################
  ############  ########
  ############  ##########
  ############  ############
  ############  ############
  ############  ##########
  ############  ########
  ####################
  ##########  ######
  ########    ####
  ######      ##
```

The geometry is generated, not drawn, so the SVG icon and the tray pixmap are
always the same shape. Getting there took a few passes: driving the triangle
deep into the bowl hollowed the D out into a bracket or a thin outline. The
overlap wants to be a *seam between two solid forms*, the way Composer's is, not
a subtraction that destroys one of them.

### Bar and menu integration

```bash
decomposer install-plugin --add-to-bar
```

Omarchy's bar loads QML plugins from `~/.config/omarchy/plugins`, so decomposer
ships one. The widget draws the mark as real rectangles tinted with the bar's own
`foreground`, so it follows the active theme instead of baking in a colour or
shipping a font glyph — and the grid is generated from `logo.py`, so the bar
button, the SVG icon and the tray pixmap cannot drift apart. `--add-to-bar` edits
`shell.json` and keeps a `.bak`; without it the command just prints what to add.

A **StatusNotifierItem** is also registered by the daemon, for desktops that have
a tray but no Omarchy plugin host. On Omarchy the plugin is the better path: a
tray icon is a foreign body in that bar, and it cannot pick up the theme.


`packaging/omarchy-menu-decomposer.jsonc` holds entries to merge into
`~/.config/omarchy/extensions/omarchy-menu.jsonc`, giving the Omarchy menu a
Camera submenu with overlay toggle and mode switching. For a keybind, bind
`decomposer toggle`.

Install the app-menu launcher with:

```bash
decomposer install-desktop
```

Use that rather than copying `packaging/decomposer.desktop` by hand. The shipped
file says `Exec=decomposer gui`, and when decomposer lives in a virtualenv that
name is not on `PATH`, so the launcher starts nothing and gives no error.
`install-desktop` writes the absolute path of the running console script and
links `~/.local/bin/decomposer`.

### Why the virtual camera speaks I420, not NV12

OBS on this system consumes `/dev/video10` only through libv4l2's "Emulated"
formats, and **libv4lconvert's NV12 conversion path flips the frame
vertically**. Measured, not surmised: a frame with a red band at rows 150–210
and a blue band at 415–450 came out of `v4lconvert_convert(NV12→BGR3)` with red
at 855–929 and blue at 630–661 — the exact mirror positions. The engine's own
output and the raw loopback bytes were verified correct first, so the flip was
isolated to that one conversion layer by running it buffer-to-buffer on a known
frame.

Publishing I420 (`YU12`) instead sidesteps the buggy code entirely: OBS,
browsers and GStreamer all consume it natively, and the bandwidth is identical.
Internally the pipeline stays NV12; only the loopback boundary de-interleaves
the UV plane. Note that v4l2loopback pins the negotiated format while any
reader holds the node, so changing the published format requires consumers to
disconnect once.

### Note on layer surfaces and dialogs

A layer surface is not an `xdg_toplevel`, so it cannot be a dialog's transient
parent. Passing it to something like `Gtk.FileDialog.open()` is a Wayland
protocol error, and the compositor's response is to disconnect the client —
which presents as the panel vanishing the instant the button is pressed, with no
error anywhere obvious. Dialogs opened from the panel must pass `None` as the
parent.

### Note on gtk4-layer-shell

The library has to be loaded into the process *before* GTK opens the Wayland
display. Importing the typelib is not enough — `ctypes.CDLL` must pull in the
shared object first, or `is_supported()` returns false and every window is
created as an ordinary toplevel the compositor centres and tiles.
