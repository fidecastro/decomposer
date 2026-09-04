# Everything decomposer can do

The complete feature list. The README stays short; this page does not.

## Two modes, one honest tradeoff

The C1 runs one of two firmwares, never both. decomposer makes the split
explicit instead of hiding it:

- **Call mode** — the camera's own firmware. UVC video **and the
  microphone**. Auto focus and white balance only; color controls
  (brightness, contrast, saturation, sharpness) plus exposure and ISO.
  Fixed 30 fps.
- **Studio mode** — stock DepthAI firmware over XLink. **Manual focus,
  manual white balance, tap-to-focus, effects, scenes, free frame
  rates** — and no microphone, because that firmware has no audio at
  all. This is a hardware fact, not a setting.

Switching takes one firmware reboot (~15 s) and always asks first. The
panel shows only the controls the current mode can actually drive, keeps
the same size in both modes, and dims everything while the feed is down.

## Capture

- Resolutions per mode: 720p, 1080p (with optional 4K capture for
  lossless zoom), 1440p and 4K in both modes; **12 MP 4:3 (4000×3000)**
  in Studio.
- Frame rate: fixed 30 in Call; **1.67–42 fps in Studio** for 16:9, up
  to 30 at 12 MP — typed into the header box, clamped to the sensor's
  real range only when you press Enter.
- Anti-banding runs from the first frame, so mains-flicker waves do not
  ride along at odd frame rates.
- Publishes clean I420 to two reader-driven virtual cameras: `/dev/video10`
  follows SEND flips, while `/dev/video11` always removes them for excluded
  apps such as Meet. Chrome, OBS, Zoom, Discord and friends consume either;
  an output nobody is watching gets one keep-warm frame a second instead of
  thirty, and the second camera is optional.

## Color

- **The fourteen Composer looks** — the eight Core Image classics and
  Opal's own five, plus none — extracted from Opal's app as measured
  3D LUTs with zero round-trip error, blended by a strength dial that
  remembers its setting per look.
- **Clarity (CLAHE)** — clip-limited local contrast on the GPU, tile
  histograms and all, for about 5% of a frame budget.
- Any `.cube` LUT dropped into `luts/` becomes a look.

## Background

- **Blur** with person segmentation, built from background-weighted
  disc taps so you never smear into your own bokeh.
- **Bokeh** — click the Blur label: highlights bloom into lens balls.
- **Replacement** — composite any image behind you instead.

## The model chain

Run the feed through your own ONNX models, several at once, each with a
live strength dial and its own CPU/CUDA choice (chosen *before* the
model loads):

- A model with a one-channel output joins the person mask.
- A model with a three-channel output recolors the frame, applied as a
  detail-preserving residual so a low-resolution model never softens
  the image.
- The chain persists across restarts; a missing model file is flagged
  and bypassed, and returns when the file does.
- In Studio mode the **default person mask runs on the camera's own
  Myriad X** — the neural engine Opal shipped and never let you use.
  The host's bundled model yields to it automatically; your added
  models keep running host-side and merge with it. `--seg-device`
  chooses: `auto` (camera in Studio, CPU otherwise), `cpu`, `cuda`, or
  `camera`.
- Or skip ONNX entirely: any process can push masks into the engine's
  `mask.sock` (u32 width, u32 height, then raw u8 frames). The internal
  model yields while you're connected.

## Framing

- **Digital zoom to 8×**, lossless to 2× when capturing 4K and
  publishing 1080p — scroll on the preview to zoom, drag to pan.
- **An independent local self-view**, mirror-like by default. **SELF MIRROR**
  controls the panel's final orientation absolutely: turning SEND flips on or
  off never changes what that button means. It does not alter either virtual
  camera.
- **Intentional output flipping**, horizontal and vertical, identical in both
  modes and clearly marked **SEND**. A second **Normal** camera removes both
  SEND flips for excluded apps (Studio's sensor orientation is corrected on
  the device).
- **Overlays** — PNG stickers and watermarks with placement, size
  limits and opacity.
- **Tap-to-focus** — click the preview in Studio and focus plus
  exposure metering aim there.

## Capture

- A **power switch** in the header (a real sliding toggle). Off does
  the only honest thing this hardware allows: it **parks the camera on
  Studio firmware**, where the microphone genuinely ceases to exist on
  the bus - Opal's firmware keeps the mic alive whether or not video
  streams, so resting there was never actually "off". Verified in
  ALSA: the C1 audio card vanishes when parked, returns when powered
  on. On re-enters your remembered mode.
- A **capture button**: click for a photo behind a 3-second on-preview
  countdown and a shutter flash; hold it for a second to start a
  recording (h264 + your default microphone, saved under
  `~/Videos/decomposer`), with a blinking REC badge on the preview and
  the same button - now a stop square - ending it. Photos land in
  `~/Pictures/decomposer`. Still capture and finalization run in the daemon,
  so replacing or closing the panel during capture cannot strand the photo.

## Presets

Everything above saves into named presets, **kept per mode** because
the two firmwares expose different controls. Hand-edited preset files
are clamped and repaired, never silently half-loaded. Loading a preset
saved in the other mode applies what it can and reports the rest. The panel
can update or delete the selected preset, and remembers the last successful
selection for each mode. On daemon launch that preset is applied before the
first engine starts; camera controls replay as soon as the firmware is ready.
The panel's **Undo** and **Redo** controls (or **Ctrl+Z** and
**Ctrl+Shift+Z**) traverse successful live adjustments and preset loads. Slider
updates made as one gesture collapse into one undo step; a new adjustment after
Undo clears the forward path. Restarts, power changes and preset deletion clear
the short in-memory history rather than pretending those destructive actions
are undone.

## The panel

A layer-shell surface dropped from the Omarchy bar, themed from your
Omarchy palette, preview on the left just like Composer. Value boxes
are typed entries that commit on Enter and never fight your typing;
sliders honor a grace period so the camera's automatics cannot yank a
control out of your hand. When the feed drops, you get a NO FEED card
or classic broadcast bars — right-click to choose. A MIC chip tells
the audio truth from ALSA, not from assumptions; a CAM chip shows whether
the camera controls beside it are live. Every action that
would reboot the camera's firmware asks first, with a real Cancel.
Drag the dotted grip beside the title to place the panel anywhere on the current
display—for example directly below a monitor-mounted camera. Its position is
remembered; double-click the grip to return to the top-right default.

## Care and feeding

- `decomposer doctor` checks the entire stack and prints a fix per
  failure.
- `decomposer install-service` writes a systemd user unit;
  `install-desktop` and `install-plugin` wire the app menu and the bar
  widget.
- A supervisor restarts the engine through every failure the camera
  has demonstrated — the crash-reboot loop, the vanished bus, the
  silent stall — with a USB hotplug watcher that notices a replug in
  seconds. All of it is replayed as unit tests in milliseconds.
