# Background blur / replacement — research and design

*Research pass 2026-08-27. The last big Composer-parity gap.*

## Model candidates

| Model | License | Input | Edge quality | Verdict |
| ----- | ------- | ----- | ------------ | ------- |
| MediaPipe Selfie Segmentation | Apache-2.0 | 256×256 (~450 KB ONNX) | good, built for video calls | **start here** |
| MODNet (portrait matting) | Apache-2.0 | 512×512 | better (true alpha, hair) | upgrade path |
| PP-HumanSeg | Apache-2.0 | 192–398px | good | alternative |
| RVM (Robust Video Matting) | **GPL-3.0** | any (recurrent) | best-in-class | **avoid**: copyleft contaminates the engine binary |
| BiRefNet | MIT | 1024px | excellent | too heavy for 30fps budget |

MediaPipe's model is a MobileNetV3 trained specifically for the
video-conference framing we serve, is 450 KB, and has clean ONNX conversions
(qualcomm/MediaPipe-Selfie-Segmentation and onnx-community on Hugging Face,
PINTO zoo #109). Vendor the ONNX under `models/` with its Apache notice.

## Runtime: `ort` (ONNX Runtime) on CPU

A 450 KB MobileNetV3 at 256×256 runs in single-digit milliseconds on CPU —
no CUDA dependency, which keeps the build portable (the Windows port is on
the roadmap) and avoids shipping a second GPU stack next to wgpu. The 4090
stays dedicated to the pixel pipeline. `tract` (pure Rust) is the fallback
if ort's binary weight offends; a CUDA/TensorRT execution provider is the
upgrade path if a heavier model ever needs it. The mask interface below is
model-agnostic either way.

## Engine integration (the mask is just another texture)

1. **Downsample tap**: the existing bilinear sampler renders an extra
   256×256 RGB target per frame; async buffer map with double buffering —
   the inference input is always one frame stale, which at 30 fps is 33 ms
   and invisible.
2. **Inference thread**: consumes the *latest* small frame (drop, never
   queue), runs the ONNX session, emits a 256×256 R8 mask.
3. **Mask upload + smoothing**: R8 texture; temporal EMA in the shader
   (`m = mix(m_prev, m_new, 0.4)`) kills flicker; bilinear upsample with an
   edge-aware weight on luma difference avoids halos at hair.
4. **Background effect in look.wgsl**: separable Gaussian at half
   resolution for blur (bokeh-ish, ~1 ms at 4K on the 4090), or an image
   texture for replacement (reuse the overlay loader). Composite by mask
   before the look/LUT stage so grading applies to the final image.
5. **Chokepoints honored**: `EngineConfig` grows live fields `blur`
   (0..1 → radius) and `background` (path|None); `engine_cli_args` /
   `engine_delta_lines` remain the only protocol authors
   (`blur 0.6`, `background /path.png|off`). Presets pick both up through
   the existing decode path.
6. **Daemon/GUI**: one slider (background blur) + one chooser (background
   image), same patterns as overlay. Studio and Call both get it — the
   effect lives in the engine, downstream of either source.

## Budget

Pipeline today runs ~2–4 ms/frame at 4K on the 4090. The blur pass adds
~1 ms; inference is off the critical path entirely. No frame-rate risk.

## Open questions

- Blur look: plain Gaussian first; a disc kernel ("bokeh") later if the
  Gaussian reads too digital.
- MODNet upgrade: only if hair edges disappoint at 256×256 — measure first.
