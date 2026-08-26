# Opal C1 + Composer notes

Captured from Felix’s 2019 Intel MacBook Pro while debugging Composer and planning decomposer.

## Hardware

| Field | Value |
|---|---|
| Product | Opal C1 |
| USB product string | `Opal C1` |
| VID:PID (running) | `03E7:F63D` (decimal vendor 999, product 63037) |
| Other IDs seen in Composer | `03E7:F63B`; Tadpole UVC `3673:0002` |
| USB speed | SuperSpeed (5 Gb/s) |
| Current | ~896 mA of 900 mA available — needs real USB 3, direct port |
| Firmware (bcd / Composer) | **4.10** |
| Serial | `1844301061E55F1700` |
| Purchased | 2023 |

`0x03E7` is the Intel Movidius / Luxonis DepthAI family VID. Composer ships `libdepthai-core.dylib` + `libusb`.

## macOS camera devices (when healthy)

1. **Opal C1** — raw UVC (`UVC Camera VendorID_999 ProductID_63037`)
2. **Opal Composer** — CMIO virtual camera (processed preview / call feed)
3. FaceTime HD — built-in

## Composer versions tried

| Version | Result on this Mac |
|---|---|
| 1.4.4 (before 2.0) | Worked |
| 2.0.0 (24) | Preview black after firmware 4.10 flash; virtual cam timed out |
| 1.4.4 reinstall (dirty) | Still black — stuck 2.0 extension “waiting to uninstall on reboot” |
| 1.4.4 after reboot + Help → Uninstall + clean reinstall | **Preview works again** |

**Lesson:** Rolling back the app is not enough; reboot to finish removing the 2.0 camera extension, then clean-uninstall and reinstall 1.4.4. Do not leave the 2023 `OpalCamera` extension enabled next to Composer.

## Architecture (Composer 1.4.4)

```
C1 USB
  ├─ DepthAI / DeviceService   (on-device: ColorCamera, ImageManip, face/hand/Meet nets, PDAF AF, LED)
  └─ UVC / UVCService          (host frames via AVFoundation)
         └─ VideoService       (MetalPetal + Core Image looks, CoreML helpers)
                └─ opalCameraExtension (CMIO virtual "Opal Composer")
```

Host looks are **separate** from DepthAI. Linux looks can sit on UVC alone.

## Look pipelines (from app Resources)

Apple Core Image photo effects (portable approximations):

- `CIPhotoEffectChrome` → chrome
- `CIPhotoEffectFade` → fade
- `CIPhotoEffectInstant` → instant
- `CIPhotoEffectMono` → mono
- `CIPhotoEffectNoir` → noir
- `CIPhotoEffectProcess` → process
- `CIPhotoEffectTonal` → tonal
- `CIPhotoEffectTransfer` → transfer

Custom MetalPetal names (need LUT / visual matching): `G1`, `D1`, `Q1`, `S1`, `X1`

VideoService also contains CLAHE and color-lookup Metal shaders.

## Bundled presets

`Default` / `A1` / `A3` / `A4` packages are mostly sticker + background metadata (`data.opaldata`, `stickers.opalstickers`). Looks are selected in the app UI, not primarily in those JSON presets.

## Legal / hygiene

- Do not redistribute Opal’s dylibs, firmware `.bin` / `.tar.gz`, CoreML `.mlmodelc`, or Metal libraries.
- decomposer reimplements looks as inspired approximations.
- Public Luxonis `depthai` is fine to probe later; Opal firmware may not speak stock protocol.

## USB troubleshooting (seen here)

- After firmware flash, C1 can vanish from the bus until a **30s** unplug.
- Dual camera extensions (2023 + Composer) → black Composer preview while raw C1 still works.
- Raw C1 can produce frames while virtual **Opal Composer** times out (extension path broken).

---

## Linux findings (2026-08-26, Arch, kernel 7.1.9)

The C1 needs **no vendor software on Linux**. It enumerates on stock `uvcvideo`
and streams immediately.

### What the OS sees

| | |
|---|---|
| V4L2 node | `/dev/video0` (capture), `/dev/video1` (metadata), `/dev/media0` |
| Card | `Opal C1: Opal C1`, serial `1844301061E55F1700`, hw rev `0x0410` (fw 4.10) |
| Pixel format | `NV12` only — no MJPEG, no YUYV |
| Modes | 3840x2160, 2560x1440, 1920x1080, 1280x720 — all advertised at 30 fps |
| Mic array | UAC2, bound to `snd-usb-audio` as card `C1` |
| Link | SuperSpeed 5 Gb/s, 896 mA, 5 interfaces |

Measured sustained capture: **1080p30 holds a solid 30 fps; 4K runs ~27 fps**,
bus-limited (3840x2160 NV12 at 30 = ~373 MB/s against a ~400 MB/s practical
USB 3.2 Gen 1 ceiling). 4K60 is not on offer and would not fit anyway.

### USB interface map

| IF | Class | Driver | Purpose |
|---|---|---|---|
| 0 | `ff` vendor | **none bound** | bulk EP `0x01` OUT / `0x81` IN, 1024 B — XLink/DepthAI channel, free for libusb |
| 1 | `0e/01` | uvcvideo | VideoControl |
| 2 | `0e/02` | uvcvideo | VideoStreaming |
| 3 | `01/01` | snd-usb-audio | Audio control |
| 4 | `01/02` | snd-usb-audio | Mic stream |

### Standard UVC controls

Working: `brightness` (0-255), `contrast` (0-100), `saturation` (0-100),
`hue` (+/-180), `gain` (100-1600), `sharpness` (0-4),
`backlight_compensation` (0-18), `power_line_frequency`,
`auto_exposure` (Auto/**Manual**), `exposure_time_absolute` (1000-33000 us).

Blocked:

- `white_balance_automatic` and `focus_automatic_continuous` are **read-only**.
  Because both stay on, `focus_absolute` (0-255) and white balance are
  permanently `inactive`. **There is no manual focus over standard UVC.**
- `white_balance_temperature` stalls with `EPIPE` when queried.

### Extension Unit 4 — probed, mostly hollow

`decomposer probe-xu` walks every selector with GET requests only. Result on
fw 4.10 (raw: `docs/xu-map-fw410.json`):

- Descriptor claims **`bNumControls` = 80**, but **`bControlSize` = 8** (64 bits).
  The two disagree; selectors 65-80 return `ENOENT` from uvcvideo because they
  fall outside `bmControls`. That is a firmware descriptor bug.
- `bmControls` is all `0xFF` (all 64 flagged) and the GUID is all `0xFF` —
  both placeholders, not a real capability declaration.
- **Selectors 9-64 answer `GET_LEN` with length 0** — declared but not implemented.
- **Selectors 1-8** report length 1, `get`+`set`, min 0 / max 255 / res 1 / def 1.

Only **selector 1 behaves like a real control**: it reads back stably (147 for
twelve consecutive reads, and across 300 ms intervals), and its value tracks
device state — 92 while idle, drifting 41-128 during streaming, 147 after a
streaming session ended.

**Selectors 2-8 are not independent controls.** They return unstable data that
usually echoes selector 1's current value, and occasionally a stale byte
(`244`, `1`, or powers of two — 8/16/32/64/128). Reading selector 5 twelve
times gave `244 x4` then `1 x8` with no write in between. `GET_MIN`/`GET_MAX`/
`GET_DEF` on the same selectors are rock stable, so the transport is fine — it
is `GET_CUR` on 2-8 that returns a shared/stale response buffer.

**Conclusion:** the XU is not where Composer's features live. It is one status
byte plus a placeholder control bank. **The real functionality — PDAF autofocus
control, the LED, HDR, the on-device neural nets — has to be behind the vendor
bulk interface (IF 0), not the XU.**

Determining what selector 1 *means*, and whether 2-8 do anything on write,
requires `SET_CUR` — the first operation in this project that writes to the
camera. Not done yet.

---

## The C1 is an OAK-1 MAX (2026-08-26)

Prior art: **[cansik/open-opal](https://github.com/cansik/open-opal)** (May 2023,
proof of concept, unmaintained since). Its central claim, which our probing
corroborates: the C1 is built on the Luxonis **LCM48 / IMX582** module and can
be driven as an **OAK-1 MAX** through the stock `depthai` framework.

Two independent cross-checks from our own UVC probe agree:

- UVC `gain` runs **100-1600** — exactly DepthAI's ISO sensitivity range.
- UVC `focus_absolute` runs **0-255** — exactly `setManualFocus()`'s lens position range.

So the UVC controls are thin wrappers over the same on-device ISP. That is
precisely why `focus_absolute` and white balance read as `inactive`: the auto
modes are locked on over UVC, but the **ISP itself accepts manual values over
XLink**.

### What DepthAI gives us that UVC cannot

`setManualFocus(0-255)`, `setAutoFocusMode(OFF)` and `setAutoFocusTrigger()`;
`setManualWhiteBalance(1000-12000 K)` and `setAutoWhiteBalanceMode(OFF)`;
`setManualExposure(exposure, iso)`. Plus live ISP feedback per frame —
`getLensPosition()`, `getSensitivity()`, `getExposureTime()`,
`getColorTemperature()`.

**This closes the manual-focus and white-balance gap entirely.**

### Device state on Linux

`depthai` 3.9.0 (which ships `cp314` wheels, so it runs on the system Python)
detects the camera at USB path `2.3` and reports its state as:

```
X_LINK_BOOTED_NON_EXCLUSIVE
```

Two consequences, both good:

1. **Already booted.** We do *not* need to upload firmware to the Myriad X. Opal's
   own firmware is running and already exposes XLink, so connecting is an attach,
   not a boot — considerably lower risk than a stock DepthAI session.
2. **Non-exclusive.** The device permits concurrent attachment, which is how
   Composer runs DeviceService and UVCService side by side on macOS. This is
   strong evidence the hybrid design works: **frames over V4L2 (kernel path,
   any app can use it) and control over XLink, at the same time.**

### The only blocker: udev

`/dev/bus/usb/002/004` is `crw-rw-r-- root root`, so libusb cannot open it
read-write and depthai logs *"Insufficient permissions to communicate with
X_LINK_BOOTED_NON_EXCLUSIVE device"* and then reports zero devices. `felix` is
in `wheel`, not any group with USB write access, and no rule for VID `03e7`
exists on the system.

Fix shipped in `packaging/60-opal-c1.rules`.

**The number in that filename is load-bearing.** `TAG+="uaccess"` only marks
the device; the ACL is applied by `/usr/lib/udev/rules.d/73-seat-late.rules`,
which runs the uaccess builtin against anything already tagged. udev rules run
in lexical order, so a `99-` file sets the tag *after* the only rule that would
act on it. The symptom is confusing: `udevadm info` shows
`CURRENT_TAGS=:uaccess:` on the device and the node still has no ACL. Numbering
the file `60-` fixes it. (Luxonis' docs use `MODE="0666"` partly because it
avoids depending on a second rule firing later.)

### Caveat

cansik targeted `depthai` 2.x. The 3.x API is reworked — `ColorCamera` became
`Camera`, `CameraBoardSocket.RGB` became `CAM_A`, and pipeline construction
changed. Treat that repo as evidence of *what the hardware allows*, not as code
to lift.

### Verified on Linux, fw 4.10 (2026-08-26)

`depthai` 3.9.0 attaches to the running device and **every manual control works,
with exact readback**:

```
streaming 1920x1080 NV12
setManualFocus( 40) -> lens reads  40   MATCH
setManualFocus(120) -> lens reads 120   MATCH
setManualFocus(200) -> lens reads 200   MATCH
setManualWhiteBalance(3000/6500/9000) -> reads 3000/6500/9000
setManualExposure(5000us, iso 200)  -> 5000us,  iso 200
setManualExposure(20000us, iso 800) -> 20000us, iso 800
auto re-engaged -> lens 135, iso 1463
```

Device reports: `sensorName: LCM48`, native **8000x6000**, `hasAutofocus: 1`,
`hasAutofocusIC: 1`, platform `RVC2`, bootloader `0.0.15`, USB `SUPER`.
cansik's identification is confirmed on current firmware.

Two cosmetic gaps: `getSensorTemperature()` returns `None`, and FOV reads a
nonsense `180.0/179.9` because the device logs *"Calibration data not found for
socket CAM_A"* — Opal never flashed Luxonis calibration data. Neither blocks anything.

### UVC and XLink are mutually exclusive

Tested directly, and this is the single most important architectural fact:

| step | result |
|---|---|
| baseline `/dev/video0` capture | OK |
| attach XLink, hold it open | `/dev/video0` **disappears** and stays gone (>25 s) |
| XLink during that window | fully healthy, streams and accepts control |
| release XLink | `/dev/video0` returns after **~14 s**, capture OK |

Attaching XLink makes the device tear down its UVC interfaces; releasing it
brings them back. **No replug is needed** — recovery is automatic in both
directions, roughly 14 s. (An earlier test appeared to show permanent loss; that
was measuring inside the re-enumeration gap.)

**Consequence: there is no hybrid design.** We cannot take frames over V4L2 and
control over XLink at the same time. Since manual focus and white balance exist
only on XLink, decomposer must pull **frames over XLink too**, and republish them
to `v4l2loopback` for the rest of the system — which is exactly what open-opal did.
The kernel capture path is off the table whenever full control is wanted.

### Why the wait is ~14 s: the C1 has two USB personalities

It is not a driver delay or a settling time. Attaching XLink makes the camera
**reboot into a different USB configuration**, and releasing it reboots back.
Watching `/sys/bus/usb/devices/2-3` across a cycle:

| moment | devnum | PID | interfaces |
|---|---|---|---|
| idle | 23 | `f63d` | **5** — vendor bulk, UVC control, UVC stream, audio control, mic |
| XLink attached | **24** | **`f63b`** | **1** — vendor bulk only |
| after release, ~0.5-7 s | — | — | **device is off the bus entirely** |
| recovered | **25** | `f63d` | 5 |

The device number increments twice per cycle, so these are genuine USB
disconnect/reconnect events, not interface rebinding. `f63b` is exactly the
second PID recorded from Composer on macOS.

- **`f63d` = camera mode.** UVC video + UAC2 mic + an idle vendor bulk interface.
- **`f63b` = DepthAI mode.** Vendor bulk only. Nothing else exists.

Measured over three runs, near-identical each time:

```
XLink connect        4.5 - 5.3 s   (/dev/video0 is already gone when it returns)
device.close()             0.14 s
/dev/video0 reappears     13.7 s
...then capturable        +1.7 s
total round trip          ~15.5 s
```

The wait is the Myriad X restarting its USB stack and re-enumerating. There is
no host-side shortcut.

### The mic dies too

`f63b` exposes one interface, so the UAC2 mic array goes with the video:

```
idle       : C1 audio card: PRESENT
xlink open : C1 audio card: GONE
after      : C1 audio card: PRESENT
```

**Running decomposer currently costs the C1's microphone**, which matters more
than the video node — a call app can be pointed at a loopback video device, but
it cannot be pointed at a microphone that does not exist.

### Open lead: interface 0 exists in camera mode too

In `f63d`, interface 0 (`2-3:1.0`, class `ff`, bulk `0x01`/`0x81`) is present and
**has no driver bound**. The mode switch is something the *DepthAI connect
sequence* triggers, not an inherent property of talking to that endpoint.

This is also the only way to reconcile Composer's macOS behaviour: it showed the
raw `Opal C1` UVC device *and* a working Composer feed at the same time, which is
impossible if it held XLink the way depthai does.

So it is worth testing whether interface 0 can be claimed with libusb while the
device stays in `f63d` — and what protocol answers there. If it can, the hybrid
design comes back, and the mic and the 15 s round trip both stop being problems.

### What actually causes the mode switch

`DEPTHAI_LEVEL=trace` settles it. depthai never talks to Opal's firmware:

```
Searching for booted device: ... X_LINK_BOOTLOADER ...
Connected bootloader version 0.0.15
Booting FW with Bootloader. Version 0.0.15, Time taken: 3009ms
```

It reboots the camera through its bootloader and loads **its own** firmware. The
device's `BoardConfig`, printed in the same trace, names both personalities
explicitly:

```json
"usb": {"flashBootedPid": 63037, "pid": 63035, "vid": 999}
"nonExclusiveMode": false, "uvc": null
```

`63037` = `0xF63D` (Opal firmware, 5 interfaces), `63035` = `0xF63B` (stock
DepthAI firmware, 1 interface). UVC and audio disappear because **stock DepthAI
firmware does not implement them**, not because XLink is inherently exclusive.

Things that do not work as escape hatches:

- `X_LINK_BOOTED_NON_EXCLUSIVE` is an **alias for `X_LINK_FLASH_BOOTED`**, the
  same enum value. There is no distinct state to request.
- Passing `X_LINK_BOOTED` fails with `X_LINK_DEVICE_NOT_FOUND` (it does at least
  leave the device alone).
- The library exposes only `DEPTHAI_INSTALL_SIGNAL_HANDLER`,
  `DEPTHAI_LIBUSB_ANDROID_JAVAVM` and `DEPTHAI_ZOO_MODELS_PATH`. No no-boot flag.
- `BoardConfig.nonExclusiveMode` configures firmware *we* boot; it cannot attach
  us to Opal's.

**depthai cannot attach to Opal's running firmware.** That is a limitation of the
client, not of the hardware.

### Interface 0 can be held without rebooting

Claiming interface 0 with libusb in `f63d` mode does **not** trigger the switch:

```
before      : pid=f63d devnum=27 nIf=5
after claim : pid=f63d devnum=27 nIf=5
UVC capture while IF0 held: OK
mic: PRESENT
```

`devnum` never changes, UVC keeps streaming, the mic stays. The endpoint is
silent until spoken to — a request/response protocol. **The hybrid is physically
possible; only the protocol is missing.**

### The mic can only exist under Opal's firmware

| | Opal fw (`f63d`) | DepthAI fw (`f63b`) |
|---|---|---|
| UVC video | yes | yes, if `BoardConfig.uvc.enable` |
| UAC2 mic | **yes** | **no — depthai has no audio support at all** |
| Manual focus / WB | no (auto locked read-only) | yes |
| Our own pipeline | no | yes |

`dai.node.UVC` and `BoardConfig.UVC` exist, so our firmware can serve video — but
nothing in depthai touches audio. The mic is an Opal firmware function.

### Why Path A is likely real

Composer on macOS showed the raw `Opal C1` UVC device, the `Opal Composer`
virtual cam, and FaceTime **simultaneously**, while linking
`libdepthai-core.dylib` and `libusb`. That is only possible if Composer attached
to Opal's firmware over XLink *without* rebooting it — which is exactly what the
state name `X_LINK_BOOTED_NON_EXCLUSIVE` describes.

So Opal's firmware almost certainly runs an XLink server alongside UVC and audio.
Reaching it needs an XLink client (protocol is public: `luxonis/XLink`) speaking
over interface 0, plus the stream names Opal's app exposes. That is the one path
that keeps video, mic and manual control at once.
