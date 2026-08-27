//! 3D colour lookup tables (.cube).
//!
//! The looks that ship with decomposer are extracted from Composer's own
//! shaders rather than hand-tuned: the reference renders were verified to be
//! pure colour transforms, so a LUT reproduces them exactly. That makes the
//! look engine open-ended too — any .cube file works, not just ours.

use anyhow::{bail, Context, Result};
use std::path::Path;

pub struct Lut {
    pub size: u32,
    /// RGB triples, red varying fastest: index = r + g*size + b*size*size.
    /// Padded to vec4 because that is how the shader binds it.
    pub data: Vec<[f32; 4]>,
}

pub fn load(path: &Path) -> Result<Lut> {
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("reading LUT {}", path.display()))?;

    let mut size = 0usize;
    let mut data: Vec<[f32; 4]> = Vec::new();

    for (n, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let mut parts = line.split_whitespace();
        let Some(head) = parts.next() else { continue };
        match head {
            "LUT_3D_SIZE" => {
                size = parts
                    .next()
                    .and_then(|v| v.parse().ok())
                    .context("LUT_3D_SIZE needs a number")?;
                if !(2..=64).contains(&size) {
                    bail!("LUT_3D_SIZE {size} out of range");
                }
                data.reserve(size * size * size);
            }
            // Titles, domains and 1D tables are accepted and ignored: the
            // domain is assumed 0..1, which is true of every LUT we ship.
            "TITLE" | "DOMAIN_MIN" | "DOMAIN_MAX" | "LUT_1D_SIZE" => {}
            _ => {
                let mut it = std::iter::once(head).chain(parts);
                let mut rgb = [0f32; 3];
                for slot in rgb.iter_mut() {
                    let Some(tok) = it.next() else {
                        bail!("{}:{}: expected three numbers", path.display(), n + 1);
                    };
                    *slot = tok.parse().with_context(|| {
                        format!("{}:{}: {tok:?} is not a number", path.display(), n + 1)
                    })?;
                }
                data.push([rgb[0], rgb[1], rgb[2], 1.0]);
            }
        }
    }

    if size == 0 {
        bail!("{}: no LUT_3D_SIZE", path.display());
    }
    let want = size * size * size;
    if data.len() != want {
        bail!(
            "{}: expected {want} entries for size {size}, found {}",
            path.display(),
            data.len()
        );
    }
    Ok(Lut { size: size as u32, data })
}

/// Look for `<dir>/<name>.cube`, case-sensitively first so D1 and d1 both work.
pub fn find(dir: &Path, name: &str) -> Option<std::path::PathBuf> {
    // Names arrive over the control socket; anything path-like would join
    // straight into the filesystem. Same-user surface, but defence in depth
    // costs one line.
    if name.contains('/') || name.contains('\\') || name.contains("..") {
        return None;
    }
    for candidate in [name.to_string(), name.to_uppercase(), name.to_lowercase()] {
        let p = dir.join(format!("{candidate}.cube"));
        if p.is_file() {
            return Some(p);
        }
    }
    None
}
