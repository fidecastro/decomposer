//! Person segmentation: the mask is a port, the bundled model an adapter.
//!
//! Three ways to produce the mask, in priority order:
//!   1. an external process connected to the mask socket (any framework:
//!      read frames from the preview socket, write masks here),
//!   2. the internal ort runner with a user-supplied --seg-model ONNX,
//!   3. the internal runner with the vendored MediaPipe model.
//! While an external client is connected the internal runner yields.
//!
//! The internal runner never queues: it consumes the *latest* downsampled
//! frame and drops the rest, so inference latency can never back up the
//! pipeline. The render thread does the 256x256 nearest-sample downscale
//! (~65k pixels, negligible) and the runner does everything else.
//!
//! Masks live in source-frame space. The shader samples them through the
//! same source_coord mapping as the video, so zoom, pan and mirror apply
//! to the mask for free.

use anyhow::{bail, Context, Result};
use std::io::Read;
use std::os::unix::net::UnixListener;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};

/// Mask smoothing: how much of the new mask enters the running average.
/// 0.45 kills single-frame flicker without visible lag at 30 fps.
const EMA_ALPHA: f32 = 0.45;

pub struct Mask {
    pub data: Vec<u8>,
    pub width: u32,
    pub height: u32,
}

#[derive(Default)]
struct Slots {
    /// Latest downsampled RGB frame (w*h*3, u8), written by the render thread.
    input: Mutex<Option<Vec<u8>>>,
    /// Latest finished mask, taken by the render thread.
    output: Mutex<Option<Mask>>,
}

pub struct Seg {
    slots: Arc<Slots>,
    cv: Arc<Condvar>,
    /// Someone downstream wants masks (blur > 0 or a background is set).
    wanted: Arc<AtomicBool>,
    /// An external producer holds the mask socket; the internal runner yields.
    external: Arc<AtomicBool>,
    /// Model input size, for the render-thread downscale.
    pub in_w: u32,
    pub in_h: u32,
}

impl Seg {
    /// Start the internal runner. Fails fast on a broken model so the error
    /// reaches the daemon log at startup, not mid-call.
    pub fn start(model_path: &str, device: &str) -> Result<Self> {
        let session = build_session(model_path, device)?;
        let (in_w, in_h, nchw) = input_shape(&session)
            .with_context(|| format!("reading input shape of {model_path}"))?;
        eprintln!(
            "seg    {model_path} {in_w}x{in_h} {} on {}",
            if nchw { "NCHW" } else { "NHWC" },
            device,
        );

        let slots = Arc::new(Slots::default());
        let cv = Arc::new(Condvar::new());
        let wanted = Arc::new(AtomicBool::new(false));
        let external = Arc::new(AtomicBool::new(false));

        let t_slots = slots.clone();
        let t_cv = cv.clone();
        let t_wanted = wanted.clone();
        let t_external = external.clone();
        std::thread::spawn(move || {
            run(session, in_w, in_h, nchw, t_slots, t_cv, t_wanted, t_external);
        });

        Ok(Self { slots, cv, wanted, external, in_w, in_h })
    }

    pub fn set_wanted(&self, wanted: bool) {
        self.wanted.store(wanted, Ordering::Relaxed);
        if wanted {
            self.cv.notify_all();
        }
    }

    pub fn wanted(&self) -> bool {
        self.wanted.load(Ordering::Relaxed) && !self.external.load(Ordering::Relaxed)
    }

    /// Render thread: hand over the latest downsampled frame (w*h*3 RGB).
    pub fn submit(&self, rgb: Vec<u8>) {
        *self.slots.input.lock().unwrap() = Some(rgb);
        self.cv.notify_all();
    }

    /// Render thread: collect a finished mask, if a new one is ready.
    pub fn take_mask(&self) -> Option<Mask> {
        self.slots.output.lock().unwrap().take()
    }

    /// Serve the mask socket. Protocol: client sends u32 width, u32 height
    /// (LE), then width*height u8 mask frames, 0 = background, 255 = person,
    /// in source-frame space. One client at a time; while one is connected
    /// the internal runner yields.
    pub fn serve_mask_socket(&self, path: String) -> Result<()> {
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path)
            .with_context(|| format!("binding mask socket {path}"))?;
        eprintln!("mask   {path} (u32 w, u32 h LE, then w*h u8 frames)");
        let slots = self.slots.clone();
        let external = self.external.clone();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                let mut header = [0u8; 8];
                if stream.read_exact(&mut header).is_err() {
                    continue;
                }
                let w = u32::from_le_bytes(header[0..4].try_into().unwrap());
                let h = u32::from_le_bytes(header[4..8].try_into().unwrap());
                if w == 0 || h == 0 || w > 4096 || h > 4096 {
                    eprintln!("mask client rejected: absurd size {w}x{h}");
                    continue;
                }
                eprintln!("mask client connected: {w}x{h}; internal runner yields");
                external.store(true, Ordering::Relaxed);
                let mut buf = vec![0u8; (w * h) as usize];
                let mut ema: Vec<f32> = Vec::new();
                while stream.read_exact(&mut buf).is_ok() {
                    let data = smooth(&mut ema, &buf);
                    *slots.output.lock().unwrap() =
                        Some(Mask { data, width: w, height: h });
                }
                external.store(false, Ordering::Relaxed);
                eprintln!("mask client left; internal runner resumes");
            }
        });
        Ok(())
    }
}

fn build_session(model_path: &str, device: &str) -> Result<ort::session::Session> {
    let builder = ort::session::Session::builder()?;
    let mut builder = match device {
        "cuda" => match builder
            .clone()
            .with_execution_providers([ort::ep::CUDA::default().build()])
        {
            Ok(b) => b,
            Err(e) => {
                eprintln!("seg: cuda unavailable ({e}); falling back to cpu");
                builder
            }
        },
        "cpu" => builder,
        other => bail!("unknown seg device {other:?} (cpu or cuda)"),
    };
    builder
        .commit_from_file(model_path)
        .with_context(|| format!("loading seg model {model_path}"))
}

/// Read (width, height, is_nchw) from the model's first input.
fn input_shape(session: &ort::session::Session) -> Result<(u32, u32, bool)> {
    let input = session
        .inputs()
        .first()
        .context("model has no inputs")?;
    let dims: Vec<i64> = match input.dtype() {
        ort::value::ValueType::Tensor { shape, .. } => {
            shape.iter().copied().collect()
        }
        other => bail!("input is not an image tensor: {other:?}"),
    };
    // Accept [1,H,W,3] (NHWC) or [1,3,H,W] (NCHW); -1 dims mean dynamic,
    // which the bundled models do not use.
    match dims.as_slice() {
        [_, h, w, 3] if *h > 0 && *w > 0 => Ok((*w as u32, *h as u32, false)),
        [_, 3, h, w] if *h > 0 && *w > 0 => Ok((*w as u32, *h as u32, true)),
        other => bail!(
            "unsupported input shape {other:?}; need [1,H,W,3] or [1,3,H,W]"
        ),
    }
}

/// EMA-smooth a fresh u8 mask against the running float average.
fn smooth(ema: &mut Vec<f32>, fresh: &[u8]) -> Vec<u8> {
    if ema.len() != fresh.len() {
        ema.clear();
        ema.extend(fresh.iter().map(|&v| v as f32));
    } else {
        for (acc, &v) in ema.iter_mut().zip(fresh) {
            *acc += (v as f32 - *acc) * EMA_ALPHA;
        }
    }
    ema.iter().map(|&v| v.clamp(0.0, 255.0) as u8).collect()
}

#[allow(clippy::too_many_arguments)]
fn run(
    session: ort::session::Session,
    in_w: u32,
    in_h: u32,
    nchw: bool,
    slots: Arc<Slots>,
    cv: Arc<Condvar>,
    wanted: Arc<AtomicBool>,
    external: Arc<AtomicBool>,
) {
    let mut session = session;
    let px = (in_w * in_h) as usize;
    let mut tensor = vec![0f32; px * 3];
    let mut ema: Vec<f32> = Vec::new();
    loop {
        let rgb = {
            let mut input = slots.input.lock().unwrap();
            loop {
                if let Some(frame) = input.take() {
                    if wanted.load(Ordering::Relaxed)
                        && !external.load(Ordering::Relaxed)
                    {
                        break frame;
                    }
                }
                input = cv.wait(input).unwrap();
            }
        };
        if rgb.len() != px * 3 {
            continue;
        }
        // u8 RGB -> f32 [0,1], in the layout the model asked for.
        if nchw {
            for i in 0..px {
                tensor[i] = rgb[i * 3] as f32 / 255.0;
                tensor[px + i] = rgb[i * 3 + 1] as f32 / 255.0;
                tensor[2 * px + i] = rgb[i * 3 + 2] as f32 / 255.0;
            }
        } else {
            for (t, &v) in tensor.iter_mut().zip(rgb.iter()) {
                *t = v as f32 / 255.0;
            }
        }
        let shape: Vec<i64> = if nchw {
            vec![1, 3, in_h as i64, in_w as i64]
        } else {
            vec![1, in_h as i64, in_w as i64, 3]
        };
        let value = match ort::value::Tensor::from_array((shape, tensor.clone())) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("seg: tensor build failed: {e}");
                continue;
            }
        };
        let name = session.inputs()[0].name().to_string();
        let outputs = match session.run(ort::inputs![name.as_str() => value]) {
            Ok(o) => o,
            Err(e) => {
                eprintln!("seg: inference failed: {e}");
                std::thread::sleep(std::time::Duration::from_millis(500));
                continue;
            }
        };
        let Some((_, out)) = outputs.iter().next() else { continue };
        let Ok(raw) = out.try_extract_tensor::<f32>() else {
            eprintln!("seg: output is not an f32 tensor");
            continue;
        };
        let (_, data) = raw;
        if data.len() < px {
            eprintln!("seg: output smaller than input plane ({})", data.len());
            continue;
        }
        // Logit or probability? Decide per frame; sigmoid is monotone so
        // this cannot flip a mask, only rescale one.
        let needs_sigmoid = data[..px]
            .iter()
            .any(|&v| !(-0.001..=1.001).contains(&v));
        let fresh: Vec<u8> = data[..px]
            .iter()
            .map(|&v| {
                let p = if needs_sigmoid { 1.0 / (1.0 + (-v).exp()) } else { v };
                (p.clamp(0.0, 1.0) * 255.0) as u8
            })
            .collect();
        let smoothed = smooth(&mut ema, &fresh);
        *slots.output.lock().unwrap() = Some(Mask {
            data: smoothed,
            width: in_w,
            height: in_h,
        });
    }
}

/// Nearest-sample an NV12 frame down to RGB at the model's input size.
/// Runs on the render thread; at 256x256 this is ~65k pixels of integer math.
pub fn downscale_nv12(nv12: &[u8], sw: u32, sh: u32, dw: u32, dh: u32) -> Vec<u8> {
    let mut out = vec![0u8; (dw * dh * 3) as usize];
    let uv_base = (sw * sh) as usize;
    for dy in 0..dh {
        let sy = dy * sh / dh;
        for dx in 0..dw {
            let sx = dx * sw / dw;
            let y_idx = (sy * sw + sx) as usize;
            let uv_idx = uv_base + ((sy / 2) * sw + (sx & !1)) as usize;
            if y_idx >= nv12.len() || uv_idx + 1 >= nv12.len() {
                continue;
            }
            let y = (nv12[y_idx] as f32 - 16.0) * (255.0 / 219.0);
            let u = (nv12[uv_idx] as f32 - 128.0) * (255.0 / 224.0);
            let v = (nv12[uv_idx + 1] as f32 - 128.0) * (255.0 / 224.0);
            let o = ((dy * dw + dx) * 3) as usize;
            out[o] = (y + 1.402 * v).clamp(0.0, 255.0) as u8;
            out[o + 1] = (y - 0.344136 * u - 0.714136 * v).clamp(0.0, 255.0) as u8;
            out[o + 2] = (y + 1.772 * u).clamp(0.0, 255.0) as u8;
        }
    }
    out
}
