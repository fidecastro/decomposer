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
- Publishes clean I420 to `/dev/video10`; Chrome, OBS, Zoom, Discord
  and friends see a camera named *decomposer*.

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
- Or skip ONNX entirely: any process can push masks into the engine's
  `mask.sock` (u32 width, u32 height, then raw u8 frames). The internal
  model yields while you're connected.

## Framing

- **Digital zoom to 8×**, lossless to 2× when capturing 4K and
  publishing 1080p — scroll on the preview to zoom, drag to pan.
- **Mirroring**, horizontal and vertical, identical in both modes
  (Studio's sensor orientation is corrected on the device).
- **Overlays** — PNG stickers and watermarks with placement, size
  limits and opacity.
- **Tap-to-focus** — click the preview in Studio and focus plus
  exposure metering aim there.

## Presets

Everything above saves into named presets, **kept per mode** because
the two firmwares expose different controls. Hand-edited preset files
are clamped and repaired, never silently half-loaded. Loading a preset
saved in the other mode applies what it can and reports the rest.

## The panel

A layer-shell surface dropped from the Omarchy bar, themed from your
Omarchy palette, preview on the left just like Composer. Value boxes
are typed entries that commit on Enter and never fight your typing;
sliders honor a grace period so the camera's automatics cannot yank a
control out of your hand. When the feed drops, you get a NO FEED card
or classic broadcast bars — right-click to choose. A MIC chip tells
the audio truth from ALSA, not from assumptions. Every action that
would reboot the camera's firmware asks first, with a real Cancel.

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
