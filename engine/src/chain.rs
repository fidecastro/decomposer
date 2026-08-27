//! The model chain: user-chosen ONNX models over the feed, each with its
//! own strength and device.
//!
//! Every model's contract is read from the model itself: an image input
//! ([1,H,W,3] or [1,3,H,W]) and either a one-channel output — a MASK model,
//! whose output joins the person mask as a weighted max — or a three-channel
//! output — a FILTER model, whose output blends into the frame at its
//! strength. Filters run in parallel on the same source frame and composite
//! in chain order; the result reaches the GPU as a detail-preserving
//! residual, so a low-resolution model recolors the full-resolution frame
//! without softening it.
//!
//! Runners never queue: each eats the latest downsampled frame and drops
//! the rest, so inference latency can never back up the pipeline. Masks and
//! layers are one frame stale, which at 30 fps is invisible.

use crate::seg::{downscale_nv12, Mask};
use anyhow::{bail, Context, Result};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Condvar, Mutex};

/// Working size for filter models with dynamic input dims.
const DYN_W: u32 = 512;
const DYN_H: u32 = 288;

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Kind {
    Mask,
    Filter,
}

/// One `--model path:device:strength` argument, parsed.
#[derive(Clone, Debug)]
pub struct Spec {
    pub path: String,
    pub device: String,
    pub strength: f32,
}

impl Spec {
    /// "path", "path:cuda" or "path:cuda:0.7". Splitting from the right
    /// keeps colons inside the path working as long as the last segments
    /// are the options.
    pub fn parse(arg: &str) -> Result<Self> {
        let mut path = arg.to_string();
        let mut device = "cpu".to_string();
        let mut strength = 1.0f32;
        if let Some((rest, last)) = path.rsplit_once(':') {
            if let Ok(v) = last.parse::<f32>() {
                strength = v.clamp(0.0, 1.0);
                path = rest.to_string();
            }
        }
        if let Some((rest, last)) = path.rsplit_once(':') {
            if last == "cpu" || last == "cuda" {
                device = last.to_string();
                path = rest.to_string();
            }
        }
        if path.is_empty() {
            bail!("empty model path in {arg:?}");
        }
        Ok(Self { path, device, strength })
    }
}

struct Runner {
    kind: Kind,
    in_w: u32,
    in_h: u32,
    /// f32 bits; live-updatable over the control socket.
    strength: Arc<AtomicU32>,
    input: Arc<Mutex<Option<Vec<u8>>>>,
    /// Latest EMA-smoothed output: mask plane or RGB image at in_w x in_h.
    output: Arc<Mutex<Option<Vec<u8>>>>,
    cv: Arc<Condvar>,
}

pub struct Chain {
    runners: Vec<Runner>,
    /// Person-mask consumers exist (blur > 0 or background set).
    mask_wanted: Arc<AtomicBool>,
    /// An external producer holds the mask socket; mask models yield.
    pub external: Arc<AtomicBool>,
    /// Canonical filter-layer size and its scratch buffers.
    layer_w: u32,
    layer_h: u32,
    layer_src: Vec<u8>,
    mask_scratch: Vec<f32>,
}

impl Chain {
    pub fn start(specs: &[Spec]) -> Result<Self> {
        let mut runners = Vec::new();
        for spec in specs {
            runners.push(start_runner(spec)?);
        }
        let layer_w = runners
            .iter()
            .filter(|r| r.kind == Kind::Filter)
            .map(|r| r.in_w)
            .max()
            .unwrap_or(DYN_W);
        let layer_h = runners
            .iter()
            .filter(|r| r.kind == Kind::Filter)
            .map(|r| r.in_h)
            .max()
            .unwrap_or(DYN_H);
        Ok(Self {
            runners,
            mask_wanted: Arc::new(AtomicBool::new(false)),
            external: Arc::new(AtomicBool::new(false)),
            layer_w,
            layer_h,
            layer_src: Vec::new(),
            mask_scratch: Vec::new(),
        })
    }

    /// Append one more model (the default person mask when the user's
    /// chain has no mask model of its own).
    pub fn push(&mut self, spec: &Spec) -> Result<()> {
        self.runners.push(start_runner(spec)?);
        Ok(())
    }

    pub fn has_mask_models(&self) -> bool {
        self.runners.iter().any(|r| r.kind == Kind::Mask)
    }

    pub fn set_mask_wanted(&self, wanted: bool) {
        self.mask_wanted.store(wanted, Ordering::Relaxed);
        if wanted {
            for r in &self.runners {
                r.cv.notify_all();
            }
        }
    }

    pub fn set_strength(&self, index: usize, strength: f32) {
        if let Some(r) = self.runners.get(index) {
            r.strength
                .store(strength.clamp(0.0, 1.0).to_bits(), Ordering::Relaxed);
            r.cv.notify_all();
        } else {
            eprintln!("model-strength: no model at index {index}");
        }
    }

    fn runner_active(&self, r: &Runner) -> bool {
        let strength = f32::from_bits(r.strength.load(Ordering::Relaxed));
        match r.kind {
            Kind::Mask => {
                strength > 0.0
                    && self.mask_wanted.load(Ordering::Relaxed)
                    && !self.external.load(Ordering::Relaxed)
            }
            Kind::Filter => strength > 0.0,
        }
    }

    /// True if any runner would consume a frame right now.
    pub fn active(&self) -> bool {
        self.runners.iter().any(|r| self.runner_active(r))
    }

    /// True while any filter model is contributing; when this goes false
    /// the GPU's residual layer must be cleared, or the last output would
    /// stay burned into the frame forever.
    pub fn filters_active(&self) -> bool {
        self.runners
            .iter()
            .any(|r| r.kind == Kind::Filter && self.runner_active(r))
    }

    /// Render thread: feed every active runner its own downscale.
    pub fn submit(&self, nv12: &[u8], w: u32, h: u32) {
        for r in &self.runners {
            if self.runner_active(r) {
                *r.input.lock().unwrap() =
                    Some(downscale_nv12(nv12, w, h, r.in_w, r.in_h));
                r.cv.notify_all();
            }
        }
    }

    /// Combined person mask: weighted max over the mask models' latest
    /// outputs, resampled to the largest mask resolution.
    pub fn take_mask(&mut self) -> Option<Mask> {
        let masks: Vec<(&Runner, Vec<u8>)> = self
            .runners
            .iter()
            .filter(|r| r.kind == Kind::Mask && self.runner_active(r))
            .filter_map(|r| r.output.lock().unwrap().take().map(|m| (r, m)))
            .collect();
        if masks.is_empty() {
            return None;
        }
        let out_w = masks.iter().map(|(r, _)| r.in_w).max().unwrap();
        let out_h = masks.iter().map(|(r, _)| r.in_h).max().unwrap();
        let px = (out_w * out_h) as usize;
        self.mask_scratch.clear();
        self.mask_scratch.resize(px, 0.0);
        for (r, m) in &masks {
            let strength = f32::from_bits(r.strength.load(Ordering::Relaxed));
            for y in 0..out_h {
                let sy = y * r.in_h / out_h;
                for x in 0..out_w {
                    let sx = x * r.in_w / out_w;
                    let v = m[(sy * r.in_w + sx) as usize] as f32 * strength;
                    let slot = &mut self.mask_scratch[(y * out_w + x) as usize];
                    *slot = slot.max(v);
                }
            }
        }
        Some(Mask {
            data: self.mask_scratch.iter().map(|&v| v as u8).collect(),
            width: out_w,
            height: out_h,
        })
    }

    /// Filter layer as a biased residual: 128 + (filtered - original) / 2.
    /// Returns (data, w, h) when at least one filter has fresh output.
    pub fn take_layer(&mut self, nv12: &[u8], w: u32, h: u32) -> Option<(Vec<u8>, u32, u32)> {
        let filters: Vec<(&Runner, Vec<u8>)> = self
            .runners
            .iter()
            .filter(|r| r.kind == Kind::Filter && self.runner_active(r))
            .filter_map(|r| r.output.lock().unwrap().take().map(|m| (r, m)))
            .collect();
        if filters.is_empty() {
            return None;
        }
        let (lw, lh) = (self.layer_w, self.layer_h);
        let px = (lw * lh) as usize;
        // Original at layer resolution, the base both for compositing and
        // for the residual.
        self.layer_src = downscale_nv12(nv12, w, h, lw, lh);
        let mut layer: Vec<f32> = self.layer_src.iter().map(|&v| v as f32).collect();
        for (r, out) in &filters {
            let strength = f32::from_bits(r.strength.load(Ordering::Relaxed));
            for y in 0..lh {
                let sy = y * r.in_h / lh;
                for x in 0..lw {
                    let sx = x * r.in_w / lw;
                    let src = ((sy * r.in_w + sx) * 3) as usize;
                    let dst = ((y * lw + x) * 3) as usize;
                    for c in 0..3 {
                        let cur = layer[dst + c];
                        layer[dst + c] =
                            cur + (out[src + c] as f32 - cur) * strength;
                    }
                }
            }
        }
        let data: Vec<u8> = (0..px * 3)
            .map(|i| {
                let residual = (layer[i] - self.layer_src[i] as f32) * 0.5 + 128.0;
                residual.clamp(0.0, 255.0) as u8
            })
            .collect();
        Some((data, lw, lh))
    }
}

fn start_runner(spec: &Spec) -> Result<Runner> {
    let session = crate::seg::build_session(&spec.path, &spec.device)?;
    let (in_w, in_h, nchw) = crate::seg::input_shape(&session)
        .with_context(|| format!("reading input shape of {}", spec.path))?;
    let kind = output_kind(&session)
        .with_context(|| format!("reading output shape of {}", spec.path))?;
    eprintln!(
        "model  {} {:?} {}x{} {} on {} strength {:.2}",
        spec.path,
        kind,
        in_w,
        in_h,
        if nchw { "NCHW" } else { "NHWC" },
        spec.device,
        spec.strength,
    );
    let strength = Arc::new(AtomicU32::new(spec.strength.to_bits()));
    let input: Arc<Mutex<Option<Vec<u8>>>> = Arc::new(Mutex::new(None));
    let output: Arc<Mutex<Option<Vec<u8>>>> = Arc::new(Mutex::new(None));
    let cv = Arc::new(Condvar::new());

    let t_in = input.clone();
    let t_out = output.clone();
    let t_cv = cv.clone();
    std::thread::spawn(move || {
        run_model(session, in_w, in_h, nchw, kind, t_in, t_out, t_cv);
    });

    Ok(Runner { kind, in_w, in_h, strength, input, output, cv })
}

/// Mask or filter? Decided by the model's output channel count.
fn output_kind(session: &ort::session::Session) -> Result<Kind> {
    let output = session.outputs().first().context("model has no outputs")?;
    let dims: Vec<i64> = match output.dtype() {
        ort::value::ValueType::Tensor { shape, .. } => shape.iter().copied().collect(),
        other => bail!("output is not a tensor: {other:?}"),
    };
    match dims.as_slice() {
        [_, 1, _, _] | [_, _, _, 1] | [_, _, _] => Ok(Kind::Mask),
        [_, 3, _, _] | [_, _, _, 3] => Ok(Kind::Filter),
        other => bail!("unsupported output shape {other:?}: need 1 (mask) or 3 (image) channels"),
    }
}

#[allow(clippy::too_many_arguments)]
fn run_model(
    mut session: ort::session::Session,
    in_w: u32,
    in_h: u32,
    nchw: bool,
    kind: Kind,
    input: Arc<Mutex<Option<Vec<u8>>>>,
    output: Arc<Mutex<Option<Vec<u8>>>>,
    cv: Arc<Condvar>,
) {
    let px = (in_w * in_h) as usize;
    let channels = match kind {
        Kind::Mask => 1,
        Kind::Filter => 3,
    };
    let mut tensor = vec![0f32; px * 3];
    let mut ema: Vec<f32> = Vec::new();
    loop {
        let rgb = {
            let mut slot = input.lock().unwrap();
            loop {
                if let Some(frame) = slot.take() {
                    break frame;
                }
                slot = cv.wait(slot).unwrap();
            }
        };
        if rgb.len() != px * 3 {
            continue;
        }
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
                eprintln!("model: tensor build failed: {e}");
                continue;
            }
        };
        let name = session.inputs()[0].name().to_string();
        let outputs = match session.run(ort::inputs![name.as_str() => value]) {
            Ok(o) => o,
            Err(e) => {
                eprintln!("model: inference failed: {e}");
                std::thread::sleep(std::time::Duration::from_millis(500));
                continue;
            }
        };
        let Some((_, out)) = outputs.iter().next() else { continue };
        let Ok((out_shape, data)) = out.try_extract_tensor::<f32>() else {
            eprintln!("model: output is not an f32 tensor");
            continue;
        };
        let needed = px * channels;
        if data.len() < needed {
            eprintln!("model: output too small ({} < {needed})", data.len());
            continue;
        }
        // Filters may answer NCHW even when fed NHWC-shaped logic; detect
        // planar layout from the output's own shape.
        let out_dims: Vec<i64> = out_shape.iter().copied().collect();
        let planar = matches!(out_dims.as_slice(), [_, 3, _, _]);
        let needs_sigmoid = data[..needed]
            .iter()
            .take(4096)
            .any(|&v| !(-0.001..=1.001).contains(&v));
        let to_u8 = |v: f32| -> u8 {
            let p = if needs_sigmoid { 1.0 / (1.0 + (-v).exp()) } else { v };
            (p.clamp(0.0, 1.0) * 255.0) as u8
        };
        let fresh: Vec<u8> = if channels == 3 && planar {
            (0..px * 3)
                .map(|i| to_u8(data[(i % 3) * px + i / 3]))
                .collect()
        } else {
            data[..needed].iter().map(|&v| to_u8(v)).collect()
        };
        let smoothed = crate::seg::smooth(&mut ema, &fresh);
        *output.lock().unwrap() = Some(smoothed);
    }
}
