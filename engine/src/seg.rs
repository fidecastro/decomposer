//! Segmentation support: ONNX session helpers, the external mask socket,
//! and the NV12 downscaler the model runners feed from.
//!
//! The runners themselves live in chain.rs — every model, including the
//! bundled MediaPipe one, is just an entry in the model chain. This module
//! owns what they share.
//!
//! Masks live in source-frame space. The shader samples them through the
//! same source_coord mapping as the video, so zoom, pan and mirror apply
//! to the mask for free.

use anyhow::{bail, Context, Result};
use std::io::Read;
use std::os::unix::net::UnixListener;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

/// Mask smoothing: how much of a fresh output enters the running average.
/// 0.45 kills single-frame flicker without visible lag at 30 fps.
const EMA_ALPHA: f32 = 0.45;

pub struct Mask {
    pub data: Vec<u8>,
    pub width: u32,
    pub height: u32,
}

pub fn build_session(model_path: &str, device: &str) -> Result<ort::session::Session> {
    let builder = ort::session::Session::builder()?;
    let mut builder = match device {
        "cuda" => match builder
            .clone()
            .with_execution_providers([ort::ep::CUDA::default().build()])
        {
            Ok(b) => b,
            Err(e) => {
                eprintln!("model: cuda unavailable ({e}); falling back to cpu");
                builder
            }
        },
        "cpu" => builder,
        other => bail!("unknown model device {other:?} (cpu or cuda)"),
    };
    builder
        .commit_from_file(model_path)
        .with_context(|| format!("loading model {model_path}"))
}

/// Read (width, height, is_nchw) from the model's first input.
pub fn input_shape(session: &ort::session::Session) -> Result<(u32, u32, bool)> {
    let input = session.inputs().first().context("model has no inputs")?;
    let dims: Vec<i64> = match input.dtype() {
        ort::value::ValueType::Tensor { shape, .. } => shape.iter().copied().collect(),
        other => bail!("input is not an image tensor: {other:?}"),
    };
    // Accept [1,H,W,3] (NHWC) or [1,3,H,W] (NCHW). Dynamic dims (-1) fall
    // back to a video-friendly working size.
    match dims.as_slice() {
        [_, h, w, 3] if *h > 0 && *w > 0 => Ok((*w as u32, *h as u32, false)),
        [_, 3, h, w] if *h > 0 && *w > 0 => Ok((*w as u32, *h as u32, true)),
        [_, 3, _, _] => Ok((512, 288, true)),
        [_, _, _, 3] => Ok((512, 288, false)),
        other => bail!("unsupported input shape {other:?}; need [1,H,W,3] or [1,3,H,W]"),
    }
}

/// EMA-smooth a fresh u8 plane against the running float average.
pub fn smooth(ema: &mut Vec<f32>, fresh: &[u8]) -> Vec<u8> {
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

/// Serve the mask socket. Protocol: client sends u32 width, u32 height
/// (LE), then width*height u8 mask frames, 0 = background, 255 = person,
/// in source-frame space. One client at a time; while one is connected the
/// chain's mask models yield (the `external` flag).
pub fn serve_mask_socket(
    path: String,
    slot: Arc<Mutex<Option<Mask>>>,
    external: Arc<AtomicBool>,
) -> Result<()> {
    let _ = std::fs::remove_file(&path);
    let listener =
        UnixListener::bind(&path).with_context(|| format!("binding mask socket {path}"))?;
    eprintln!("mask   {path} (u32 w, u32 h LE, then w*h u8 frames)");
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
            eprintln!("mask client connected: {w}x{h}; internal mask models yield");
            external.store(true, Ordering::Relaxed);
            let mut buf = vec![0u8; (w * h) as usize];
            let mut ema: Vec<f32> = Vec::new();
            while stream.read_exact(&mut buf).is_ok() {
                let data = smooth(&mut ema, &buf);
                *slot.lock().unwrap() = Some(Mask { data, width: w, height: h });
            }
            external.store(false, Ordering::Relaxed);
            eprintln!("mask client left; internal mask models resume");
        }
    });
    Ok(())
}

/// Nearest-sample an NV12 frame down to RGB. Runs on the render thread; at
/// model input sizes this is tens of thousands of pixels of integer math.
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
