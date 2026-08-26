# How the reference set was actually made (2026-08-26)

The files in `references/look-*.png` did not come from a camera. This is the
rundown of why, and how they were produced, so the work can continue on another
machine without re-deriving any of it.

## The plan that failed

`docs/reference-capture.md` describes the original protocol: photograph
`color-target.png` off a monitor through Composer's virtual camera, once per
look, and fit LUTs to the pairs. Two attempts on the Intel MacBook (Iris Plus
645, Composer 1.4.4) went nowhere, for two independent reasons:

1. **The one capture that existed was unusable.** The camera baseline clipped
   36% of the colour cube — both ends at once, red and blue far harder than
   green (auto-exposure plus oversaturation). Differencing cannot recover a
   patch that reads 0 or 255 in both frames; that information is destroyed, not
   distorted. `scripts/check_reference.py` now exists so this gets caught at
   capture time instead of at fit time.

2. **Composer never applies looks on Intel Macs — at all.** The Filters module
   is absent from the sidebar (every section, tab and menu was walked), and the
   gate is at the render layer, not just the UI: writing
   `"filters": {"selected": "M1", "enabled": true, "intensity": 1}` directly
   into a preset's `data.opaldata` is *read* by the app (the Effects toggle
   lights up from the file) but ignored by the renderer, through a full app
   restart. The app's own strings say why: *"This feature requires an M1 or
   later system."* The protocol was unwinnable on this hardware.

## The plan that worked

The looks are ordinary code inside the installed app, and the GPU runs them
fine — only the app refuses to. Two facts, both verified on this machine:

- The five custom looks are **unary MetalPetal fragment shaders** —
  `MTG1Fragment`, `MTD1Fragment`, `MTQ1Fragment`, `MTS1Fragment`,
  `MTX1Fragment` — inside
  `Opal Composer.app/Contents/XPCServices/OpalCameraVideoService.xpc/Contents/Resources/default.metallib`.
  Pipeline reflection shows their entire interface: one input texture, one
  sampler, nothing else. Pure colour transforms.
- The eight named looks are Apple's **public `CIPhotoEffect*` filters**,
  exactly as the app's `*_pipeline.json` resources record (`noir` →
  `CIPhotoEffectNoir`, etc.). Display names: C1, D1, F1, G1, I2, M1, N2, P1,
  Q1, S1, T1, T2, X1.

`scripts/render_looks_mac.swift` renders `references/color-target.png` through
all thirteen — the Metal five with a passthrough vertex shader paired against
Opal's own fragments, the Core Image eight through `CIFilter` with colour
management pinned off so bytes pass through as the video path sees them. Plus
`look-off.png`, which is byte-identical to the target.

The result is strictly better than what the camera protocol could ever have
produced: pixel-exact pairs, zero framing drift, all 4096 patches measurable,
and none of the display-gamut or sensor-response distortion the photographed
route was designed to merely tolerate.

```bash
swiftc -O scripts/render_looks_mac.swift -o scripts/render_looks_mac
./scripts/render_looks_mac references/color-target.png references/
for l in G1 D1 Q1 S1 X1 chrome fade instant mono noir process tonal transfer; do
  python scripts/check_reference.py --exact references/look-$l.png
done
```

`--exact` matters: a look that crushes shadows to its floor (G1 maps 24% of
patches to a rail — that navy shadow floor is *the look*) is behaving, not
being measured badly, so only fiducials and framing gate.

## What the renders showed

| Look | Behaviour |
|---|---|
| G1 | colour grade; shadows lifted/crushed into a cool navy floor |
| D1, Q1 | grayscale looks |
| S1, X1 | colour grades; X1 warm-purple lift, higher chroma |
| mono, noir, tonal | grayscale (as the CI filter names imply) |
| chrome, fade, instant, process, transfer | colour grades |

## Caveats and hygiene

- **Intensity.** Composer's preset schema defaults `filters.intensity` to 0.5,
  so the in-app look users see may be a 50% blend with the input. The renders
  measure the shaders at full strength — the right thing to fit a LUT to; the
  blend is a plain per-pixel mix the engine can apply after the LUT.
- **Nothing of Opal's is redistributed.** The metallib is read from the locally
  installed app at run time; only the rendered PNGs are kept. Never commit the
  metallib, dylibs, or firmware.
- **The camera protocol still has one job**: on an M1 Mac with Composer
  actually applying looks, photographing the virtual camera is the way to
  verify that real-world output matches what the LUTs predict.
  `scripts/capture_mac_refs.sh` + `check_reference.py` (without `--exact`) are
  ready for that, and the frame grabber now settles for 3 s and locks whatever
  the device allows before keeping a frame.

## What happens next

Unchanged from `reference-capture.md`: sample every patch centre of each pair,
fit a 33³ `.cube` LUT per look, load them through the engine's LUT path. The
fit no longer needs perspective correction or clipping tolerance — the pairs
are aligned by construction.
