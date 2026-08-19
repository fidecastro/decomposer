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
