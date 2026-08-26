# Capturing reference stills on the Mac

What we need, and why it has to be done this particular way.

> **Status (2026-08-26): the reference set exists, and no camera was involved.**
> Composer 1.4.4 on an Intel Mac never applies looks: the Filters UI is absent,
> and a preset whose `effects.filters` block names a look is read but ignored at
> render time — the app's own strings say why ("This feature requires an M1 or
> later system"). The camera protocol below therefore cannot even be started on
> Intel hardware.
>
> Instead, `scripts/render_looks_mac.swift` renders `references/color-target.png`
> through Composer's *actual* look implementations, which any Metal GPU can run:
> the five custom looks are unary MetalPetal fragment shaders
> (`MTG1Fragment` …) in OpalCameraVideoService's `default.metallib` — one input
> texture, no other parameters — and the eight named looks are Apple's public
> `CIPhotoEffect*` Core Image filters, exactly as the app's `*_pipeline.json`
> files record. The resulting pairs are pixel-exact, cover the whole cube, and
> carry none of the display-gamut / sensor-response / clipping distortions the
> photographed protocol was designed to merely tolerate. Validate them with
> `check_reference.py --exact` (clipping in a look's *output* is the look
> behaving, so only fiducials and framing gate).
>
> The metallib is read from the locally installed app at run time and only the
> rendered PNGs are kept — never commit Opal's own binaries or shaders.
>
> One honest caveat: this measures the shaders at full strength. Composer's
> preset schema defaults `filters.intensity` to 0.5, so the in-app look as users
> see it may be a 50% blend with the input; that is a plain per-pixel mix the
> engine can apply after the LUT.
>
> The protocol below is kept for what the synthetic path cannot do: verifying on
> an M1 Mac that the virtual-camera output through a real display and sensor
> matches what the LUTs predict.

## What a reference still is *for*

Composer's five custom looks — `G1`, `D1`, `Q1`, `S1`, `X1` — are Metal shaders
we cannot read. The only way to reproduce them is to measure what they *do*:
feed a colour in, see what comes out, and fit a 3D LUT to those pairs.

So a reference still is not "a nice photo with the look on". It is one half of a
**pair**: the same scene, captured twice, differing *only* by the look. Without
the matching baseline there is nothing to compare against and the capture is
useless.

## The three things that make or break it

**1. Both captures must come from the same device.**

Capture the baseline through Composer's virtual camera with the look set to
*off* — not from the raw `Opal C1` device. The virtual camera may also scale,
sharpen or otherwise touch the image, and if the baseline comes from a different
path we would be measuring those differences too and baking them into the LUT.
Same path, same everything, only the look changes.

**2. The scene must cover the colour cube.**

A face against a wall constrains a tiny region of colour space. A LUT fitted
from it is unconstrained everywhere else and would invent the rest. Use
`references/color-target.png` — 4096 patches walking the whole RGB cube, plus a
skin-tone strip, plus corner fiducials so the grid can be located in the
photograph.

**3. Nothing may move or auto-adjust between captures.**

Every capture in a set must be pixel-comparable. One autofocus hunt or one
auto-exposure correction between shots and the pairing is broken.

## Procedure

1. Display `references/color-target.png` **full screen** on a monitor, at 100%
   scale, with night-light/true-tone **off** and brightness fixed.
2. Put the C1 on something solid, pointed at the screen so the target fills as
   much of the frame as possible and as square-on as you can manage. Some
   perspective is fine — the fiducials let us correct for it.
3. In Composer: set **manual** exposure, **manual** white balance, **manual**
   focus. Turn off anything automatic. No stickers, no background replacement,
   no other adjustments — only the look should change from here on.

   This has to be done in Composer's own UI. The virtual camera reports *no*
   lockable exposure, white-balance or focus modes to AVFoundation, so the
   capture tool cannot pin any of them for you — it can only wait for them to
   settle. Over standard UVC the C1's auto white balance and continuous
   autofocus are read-only and stay on (`docs/camera-notes.md`), so if Composer
   does not expose these controls, that is the ceiling on this path.

4. Expose for the *chart*, not for a pleasing picture. Both ends must survive:
   check that the darkest patches are not sitting at 0 and the brightest are
   not sitting at 255. If red and blue clip while green does not, saturation is
   too high — the look is being measured through a curve that has already
   thrown the corners of the cube away.

5. Capture the baseline with the look **off**:

   ```bash
   ./scripts/capture_mac_refs.sh look off
   ```

6. Then, without touching the camera, the screen, or any other setting, select
   each look in the Composer UI and capture it:

   ```bash
   ./scripts/capture_mac_refs.sh look G1
   ./scripts/capture_mac_refs.sh look D1
   ./scripts/capture_mac_refs.sh look Q1
   ./scripts/capture_mac_refs.sh look S1
   ./scripts/capture_mac_refs.sh look X1
   ```

7. The eight Core Image looks are worth capturing too — we approximated those by
   hand and have never checked them against the real thing:

   ```bash
   for l in chrome fade instant mono noir process tonal transfer; do
     ./scripts/capture_mac_refs.sh look "$l"    # select it in the UI first
   done
   ```

8. Finally, a sanity set: point the camera at a normal scene (you, a room) and
   capture `off` plus two or three looks. The chart tells us the numbers; a real
   scene tells us whether the result *looks* right, which is not the same thing.

Run the captures from your own Terminal. macOS attributes camera access to the
app that launched the process, so a capture driven from somewhere else gets a
session that starts cleanly and then delivers no frames at all — it looks like a
hang, not a permission error.

Each capture is checked as it is taken:

```bash
./scripts/check_reference.py references/look-G1.png
```

It locates the fiducials, samples all 4096 patches, and reports how much of the
cube was destroyed by clipping — plus, for anything other than the baseline, how
far the framing drifted. `capture_mac_refs.sh` runs it automatically and stops
on failure. A capture that clips is not recoverable by differencing: if the
baseline reads 255 and the look reads 255, that colour is gone rather than
merely distorted. Aim for **under 5%** destroyed.

## What good output looks like

`references/look-off.png` plus one `references/look-<name>.png` per look, all the
same resolution, all showing the identical framing. If the framing shifts between
shots, that set has to be redone — a chart that has moved cannot be paired.

PNG only. A JPEG's ringing around the hard patch edges would be measured as if
it were part of the look.

## What happens next

For each look we locate the chart via the fiducials, sample every patch centre in
both images, and fit a 33³ `.cube` LUT to the resulting pairs. The LUT then loads
into the same shader path as everything else — which is why LUT support is worth
building whether or not these captures ever happen: it makes the look engine
open-ended instead of a fixed list.

Note that this measures the look *as the camera saw the screen*, so it inherits
the display's gamut and the camera's own response. That is acceptable: both
captures share those distortions, and fitting on the difference cancels most of
it. It will not be exact, but it will be measured rather than invented.
