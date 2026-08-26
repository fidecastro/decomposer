# Capturing reference stills on the Mac

What we need, and why it has to be done this particular way.

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
4. Capture the baseline with the look **off**:

   ```bash
   ./scripts/capture_mac_refs.sh look off
   ```

5. Then, without touching the camera, the screen, or any other setting, select
   each look in the Composer UI and capture it:

   ```bash
   ./scripts/capture_mac_refs.sh look G1
   ./scripts/capture_mac_refs.sh look D1
   ./scripts/capture_mac_refs.sh look Q1
   ./scripts/capture_mac_refs.sh look S1
   ./scripts/capture_mac_refs.sh look X1
   ```

6. The eight Core Image looks are worth capturing too — we approximated those by
   hand and have never checked them against the real thing:

   ```bash
   for l in chrome fade instant mono noir process tonal transfer; do
     ./scripts/capture_mac_refs.sh look "$l"    # select it in the UI first
   done
   ```

7. Finally, a sanity set: point the camera at a normal scene (you, a room) and
   capture `off` plus two or three looks. The chart tells us the numbers; a real
   scene tells us whether the result *looks* right, which is not the same thing.

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
