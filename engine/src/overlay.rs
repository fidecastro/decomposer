//! Image overlays: logos, watermarks, lower-thirds.
//!
//! Composer called these stickers. The host decodes and scales the image once,
//! then hands the GPU a buffer that is already at its final size, so the shader
//! only does a rectangle test and a blend — no sampling maths in the hot path,
//! and no resampling per frame.

use anyhow::{bail, Context, Result};
use std::fs::File;
use std::io::BufReader;

pub struct Overlay {
    pub width: u32,
    pub height: u32,
    /// RGBA8, one pixel per u32, little-endian: R | G<<8 | B<<16 | A<<24.
    pub pixels: Vec<u32>,
}

impl Overlay {
    pub fn is_empty(&self) -> bool {
        self.width == 0 || self.height == 0
    }
}

/// Decode a PNG and scale it to fit within `max_w` x `max_h`, keeping aspect.
/// Either bound may be zero to mean "no constraint on this axis".
pub fn load(path: &str, max_w: u32, max_h: u32) -> Result<Overlay> {
    let decoder = png::Decoder::new(BufReader::new(
        File::open(path).with_context(|| format!("opening overlay {path}"))?,
    ));
    let mut reader = decoder.read_info().context("reading PNG header")?;
    let size = reader
        .output_buffer_size()
        .context("PNG dimensions are too large to allocate")?;
    let mut raw = vec![0u8; size];
    let info = reader.next_frame(&mut raw).context("decoding PNG")?;

    let (sw, sh) = (info.width, info.height);
    if sw == 0 || sh == 0 {
        bail!("{path}: image has zero size");
    }

    // Normalise whatever the file used into straight RGBA8.
    let src = to_rgba8(&raw[..info.buffer_size()], sw, sh, info.color_type, info.bit_depth)
        .with_context(|| format!("{path}: unsupported PNG format"))?;

    let (dw, dh) = fit(sw, sh, max_w, max_h);
    let pixels = if (dw, dh) == (sw, sh) {
        src
    } else if dw < sw {
        box_downscale(&src, sw, sh, dw, dh)
    } else {
        nearest_upscale(&src, sw, sh, dw, dh)
    };
    Ok(Overlay { width: dw, height: dh, pixels })
}

fn fit(sw: u32, sh: u32, max_w: u32, max_h: u32) -> (u32, u32) {
    if max_w == 0 && max_h == 0 {
        return (sw, sh);
    }
    let sx = if max_w == 0 { f64::INFINITY } else { max_w as f64 / sw as f64 };
    let sy = if max_h == 0 { f64::INFINITY } else { max_h as f64 / sh as f64 };
    let scale = sx.min(sy);
    if !scale.is_finite() || scale <= 0.0 {
        return (sw, sh);
    }
    (
        ((sw as f64 * scale).round() as u32).max(1),
        ((sh as f64 * scale).round() as u32).max(1),
    )
}

fn to_rgba8(
    raw: &[u8],
    w: u32,
    h: u32,
    color: png::ColorType,
    depth: png::BitDepth,
) -> Option<Vec<u32>> {
    if depth != png::BitDepth::Eight {
        return None; // 16-bit and sub-byte depths are rare for overlays
    }
    let n = (w * h) as usize;
    let mut out = Vec::with_capacity(n);
    let pack = |r: u8, g: u8, b: u8, a: u8| {
        (r as u32) | ((g as u32) << 8) | ((b as u32) << 16) | ((a as u32) << 24)
    };
    match color {
        png::ColorType::Rgba => {
            for p in raw.chunks_exact(4).take(n) {
                out.push(pack(p[0], p[1], p[2], p[3]));
            }
        }
        png::ColorType::Rgb => {
            for p in raw.chunks_exact(3).take(n) {
                out.push(pack(p[0], p[1], p[2], 255));
            }
        }
        png::ColorType::GrayscaleAlpha => {
            for p in raw.chunks_exact(2).take(n) {
                out.push(pack(p[0], p[0], p[0], p[1]));
            }
        }
        png::ColorType::Grayscale => {
            for &v in raw.iter().take(n) {
                out.push(pack(v, v, v, 255));
            }
        }
        _ => return None,
    }
    Some(out)
}

/// Average the source pixels covering each destination pixel.
///
/// Nearest sampling wrecks a downscaled logo — thin strokes drop out entirely.
/// Alpha is weighted into the colour so transparent pixels do not drag dark
/// fringes into the edges.
fn box_downscale(src: &[u32], sw: u32, sh: u32, dw: u32, dh: u32) -> Vec<u32> {
    let mut out = Vec::with_capacity((dw * dh) as usize);
    for dy in 0..dh {
        let y0 = dy * sh / dh;
        let y1 = (((dy + 1) * sh + dh - 1) / dh).min(sh).max(y0 + 1);
        for dx in 0..dw {
            let x0 = dx * sw / dw;
            let x1 = (((dx + 1) * sw + dw - 1) / dw).min(sw).max(x0 + 1);
            let (mut r, mut g, mut b, mut a, mut n) = (0f64, 0f64, 0f64, 0f64, 0f64);
            for sy in y0..y1 {
                for sx in x0..x1 {
                    let p = src[(sy * sw + sx) as usize];
                    let pa = ((p >> 24) & 0xff) as f64 / 255.0;
                    r += ((p & 0xff) as f64) * pa;
                    g += (((p >> 8) & 0xff) as f64) * pa;
                    b += (((p >> 16) & 0xff) as f64) * pa;
                    a += pa;
                    n += 1.0;
                }
            }
            let (rr, gg, bb) = if a > 0.0 {
                (r / a, g / a, b / a)
            } else {
                (0.0, 0.0, 0.0)
            };
            let aa = (a / n * 255.0).round().clamp(0.0, 255.0) as u32;
            out.push(
                (rr.round().clamp(0.0, 255.0) as u32)
                    | ((gg.round().clamp(0.0, 255.0) as u32) << 8)
                    | ((bb.round().clamp(0.0, 255.0) as u32) << 16)
                    | (aa << 24),
            );
        }
    }
    out
}

fn nearest_upscale(src: &[u32], sw: u32, sh: u32, dw: u32, dh: u32) -> Vec<u32> {
    let mut out = Vec::with_capacity((dw * dh) as usize);
    for dy in 0..dh {
        let sy = (dy * sh / dh).min(sh - 1);
        for dx in 0..dw {
            let sx = (dx * sw / dw).min(sw - 1);
            out.push(src[(sy * sw + sx) as usize]);
        }
    }
    out
}
