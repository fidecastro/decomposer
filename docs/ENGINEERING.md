# Engineering notes: the hard questions, and how they were answered

This is the document for agents and developers. decomposer was built by
asking a camera questions and believing only measured answers. Below are
the questions that fought back, each with the resolution that shipped.
The raw findings live in [camera-notes.md](camera-notes.md); the
architecture is enforced by the test suite, not by good intentions.

## 1. What is this camera, really?

An Opal C1 is a Luxonis OAK-1 MAX wearing a nice enclosure: LCM48/IMX582
sensor, Myriad X. It runs exactly one of two firmwares. `03e7:f63d` is
Opal's personality — UVC video plus a UAC2 microphone. `03e7:f63b` is
stock DepthAI, reached through the `f63c` bootloader — an XLink device
with manual ISP control and **no audio support at all**. The modes are
mutually exclusive and a switch costs a ~15 s firmware reboot.

**Resolution:** stop fighting it. Two explicit user-facing modes (Call
and Studio), a routing table in `core/model.py` that says which control
each mode can reach, and a UI that draws only what the current firmware
can drive. The mic-versus-manual-focus tradeoff is presented as the
hardware fact it is.

## 2. Why does Call mode sometimes die 11 seconds into streaming?

The infamous "degraded state." Investigated to ground truth in one
sitting: WirePlumber was exonerated twice (crashes continued with it
stopped and only our engine on the node), the USB3 NO_LPM quirk was
plausible but unproven (stable boots occurred with and without it), and
power-off rituals cured nothing. The truth: **each Opal-firmware boot is
a coin toss.** A bad boot dies seconds after streaming starts (kernel:
`xhci WARN Set TR Deq Ptr` then ENODEV, fall to bootloader); a good boot
streams indefinitely.

**Resolution:** a supervision policy (`core/health.py`) that retries
until the firmware wins its toss, recognizes the dies-young pattern,
holds off a camera that has vanished from the bus, and detects silent
stalls by frame progress. Every observed failure is replayed as a unit
test in milliseconds. A netlink uevent watcher wakes every hold the
moment the camera re-enumerates — debounced, and taught not to mistake
the crash loop's own re-enumerations for a human replugging the cable
(that mistake briefly defeated the sick-hold; the fix is pinned by
test).

## 3. Why did every mode switch cost two firmware reboots?

A three-part race found by counting enumerations in the kernel log: the
supervisor treated the switch's own engine-stop as a death, the hotplug
watcher woke the retry hold early (a switch's re-enumeration is
indistinguishable from a replug), and the retry never re-checked whether
the engine was already alive.

**Resolution:** the supervisor ignores engine state entirely while a
client transition owns the camera, re-checks liveness after every hold,
and each completed transition clears the camera event its own
enumerations armed. Verified: one switch, one bootloader pass, one
firmware enumeration, silence after.

## 4. Why was the published image upside down (only sometimes)?

Two separate inversions, both measured. Studio's sensor is mounted
upside down and stock firmware does not correct it (correlating the two
modes' output scored +0.80 for rotate-180); fixed with an on-device
rotation so both modes share one orientation. Separately,
libv4lconvert's NV12-to-BGR path flips vertically — measured by finding
mirror bands at exact row offsets — so consumers that negotiated
emulated formats saw a flipped feed.

**Resolution:** publish I420 (which libv4lconvert passes through
honestly) and correct orientation at the source. Note for the field:
OBS holds `/dev/video10` from its tray and pins the negotiated format —
"close OBS" means the tray icon too.

## 5. Where did the Composer looks come from, with no reverse engineering?

Color charts were rendered through Opal's own app on a Mac, once per
look, and diffed against the unprocessed chart. The difference *is* the
look: distilled into 3D `.cube` LUTs and verified by round-tripping the
chart through the LUT — maximum error zero.

**Resolution:** thirteen measured LUTs plus a built-in fallback,
loaded asynchronously (a slow disk is a late look, never a frame
hitch), applied in a single WGSL compute pass alongside zoom, CLAHE,
masking and compositing.

## 6. What frame geometry does the sensor actually deliver?

The sensor advertises three configs: 3840×2160 at 1.67–42 fps,
4000×3000 at up to 30, 5312×6000 at up to 10. Two traps inside that
menu. First, Call mode's UVC descriptors ignore all of it: four 16:9
modes, 30 fps, NV12, take it or leave it. Second, request 5312×6000 as
NV12 and depthai **silently delivers 4000×3000** — the config is
RAW-only, the ISP tops out at 12 MP, and slicing an 18 MB/frame stream
at 47.8 MB boundaries is exactly a garbage feed.

**Resolution:** the sensor facts live in `core/model.py`
(`SENSOR_CONFIGS`, `fps_limits`, `resolutions_for`) and every selector
derives from them; 32 MP is deliberately not offered; and the depthai
adapter raises loudly on any delivered-geometry mismatch so a silent
downgrade can never reach the loopback again. Wave-like banding at odd
frame rates turned out to be mains flicker; anti-banding AUTO ships
enabled.

## 7. How do you let users run *any* model without marrying a runtime?

**Resolution:** the mask is a port, not a feature. The engine consumes
masks; where they come from is pluggable at three levels — the bundled
MediaPipe ONNX (Apache-2.0, via ort on CPU), any user ONNX with an
image input and a 1- or 3-channel output (shapes autodetected, device
chosen before load), or an external process writing raw masks into
`mask.sock` from any framework. Image-transforming models apply as a
detail-preserving residual: their color work lands at full resolution,
and physics keeps low-res models from pretending to do structure.
Masks live in source-frame space and ride the same coordinate mapping
as the video, so zoom, pan and mirror apply to them for free.

## 8. How does the code stay honest?

Hexagonal, with tripwires. `core/` is pure domain fact and policy — an
AST test forbids it IO imports. Hardware lives behind `ports.py`
Protocols in `adapters/`; the daemon is the application layer; the
panel and CLI speak only to the daemon. The chokepoints are enforced,
not aspirational:

- Engine argv and the control-socket protocol are two projections of
  one `EngineConfig`; the thirteen protocol verbs may be composed only
  in `core/model.py`, and a brace-anchored regex test fails the build
  on drift.
- The supervision policy is pure decisions over facts; the daemon
  executes them. That split is why the failure zoo is a fast test
  suite instead of an evening with a screwdriver.
- The audit habit pays: the last sweep caught a hand-copied CLI
  resolution map still offering 32 MP an hour after the core dropped
  it. It now derives from the core, with a test pinning the
  derivation.

## Where to dig

- [camera-notes.md](camera-notes.md) — the raw lab notebook: USB
  descriptor dumps, timing measurements, the degraded-state timeline.
- [background-blur.md](background-blur.md) — the segmentation design,
  model survey and license verdicts.
- [ROADMAP.md](ROADMAP.md) — the hardening history and what remains.
- `tests/` — the architecture tripwires and the hardware failure zoo.
