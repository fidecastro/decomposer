//! The engine's one config: argv and the control socket both feed this.
//!
//! Mirror of the Python side's EngineConfig chokepoint. A fresh engine gets
//! its state from command-line flags; a running one gets deltas as text
//! lines — but both end up as mutations of this struct, applied by one
//! render-loop path, so the two ways in cannot drift apart.

use std::sync::{Arc, Mutex};

/// What the render loop reads each frame. `dirty` avoids re-uploading the
/// uniform when nothing has changed.
#[derive(Debug, Clone)]
pub struct Config {
    pub look: u32,
    /// The name as requested. Resolved to a LUT file if one exists, and only
    /// then falling back to a built-in curve.
    pub look_name: String,
    pub strength: f32,
    /// bit 0: mirror horizontally, bit 1: mirror vertically.
    pub flip: u32,
    pub dirty: bool,
    /// Overlay is tracked separately because applying it means decoding and
    /// rescaling a file, which must not happen on every uniform tweak.
    pub overlay_path: Option<String>,
    pub overlay_x: u32,
    pub overlay_y: u32,
    pub overlay_max_w: u32,
    pub overlay_max_h: u32,
    pub overlay_opacity: f32,
    pub overlay_dirty: bool,
    pub zoom: f32,
    pub pan_x: f32,
    pub pan_y: f32,
    pub clahe: f32,
    /// Background blur strength (0 = off, and segmentation idles).
    pub blur: f32,
    /// 0 = smooth blur, 1 = bokeh (highlight-weighted disc).
    pub blur_style: u32,
    /// Background replacement image; like the overlay, loading is a file
    /// decode and must not run on every uniform tweak.
    pub bg_path: Option<String>,
    pub bg_dirty: bool,
    /// Per-model strengths for the model chain, sparse updates: (index,
    /// value) pairs drained by the render loop each frame.
    pub model_strengths: Vec<(usize, f32)>,
}

pub type Shared = Arc<Mutex<Config>>;

pub fn shared(look: u32, name: String, strength: f32, flip: u32) -> Shared {
    Arc::new(Mutex::new(Config {
        look,
        look_name: name,
        strength,
        flip,
        dirty: false,
        overlay_path: None,
        overlay_x: 0,
        overlay_y: 0,
        overlay_max_w: 0,
        overlay_max_h: 0,
        overlay_opacity: 1.0,
        overlay_dirty: false,
        zoom: 1.0,
        pan_x: 0.0,
        pan_y: 0.0,
        clahe: 0.0,
        blur: 0.0,
        blur_style: 0,
        bg_path: None,
        bg_dirty: false,
        model_strengths: Vec::new(),
    }))
}

/// Apply one control line. The only parser of the runtime protocol.
///
/// Every accepted command is logged with its text: the socket accepts lines
/// from any same-user process with no provenance, and an unattributed
/// command was once observed in the wild — at least the log now says what
/// arrived and when.
pub fn apply_line(line: &str, state: &Shared) {
    let line = line.trim();
    let mut parts = line.splitn(2, char::is_whitespace);
    let Some(cmd) = parts.next() else { return };
    let rest = parts.next().unwrap_or("").trim();
    if rest.is_empty() {
        eprintln!("control command {cmd:?} needs an argument");
        return;
    }
    let Ok(mut s) = state.lock() else { return };
    let accepted = match cmd {
        "look" => {
            // Any name is accepted here: resolution to a LUT file or a
            // built-in curve happens in the render loop, which is the only
            // place that knows where the LUTs live.
            s.look_name = rest.to_string();
            s.look = crate::gpu::look_index(rest).unwrap_or(0);
            s.dirty = true;
            true
        }
        "strength" => match rest.parse::<f32>() {
            Ok(v) => {
                s.strength = v.clamp(0.0, 1.0);
                s.dirty = true;
                true
            }
            Err(_) => false,
        },
        "flip" => match rest.parse::<u32>() {
            Ok(v) => {
                s.flip = v & 3;
                s.dirty = true;
                true
            }
            Err(_) => false,
        },
        "overlay" => {
            // The rest of the line is the path, so filenames may contain spaces.
            s.overlay_path = if rest == "off" { None } else { Some(rest.to_string()) };
            s.overlay_dirty = true;
            true
        }
        "overlay-rect" => {
            let nums: Vec<u32> = rest
                .split_whitespace()
                .filter_map(|v| v.parse().ok())
                .collect();
            if nums.len() == 4 {
                s.overlay_x = nums[0];
                s.overlay_y = nums[1];
                s.overlay_max_w = nums[2];
                s.overlay_max_h = nums[3];
                s.overlay_dirty = true;
                true
            } else {
                eprintln!("overlay-rect needs: x y max_w max_h");
                false
            }
        }
        "clahe" => match rest.parse::<f32>() {
            Ok(v) => {
                s.clahe = v.clamp(0.0, 1.0);
                s.dirty = true;
                true
            }
            Err(_) => false,
        },
        "zoom" => match rest.parse::<f32>() {
            Ok(v) => {
                s.zoom = v.clamp(1.0, 8.0);
                s.dirty = true;
                true
            }
            Err(_) => false,
        },
        "pan" => {
            let nums: Vec<f32> = rest
                .split_whitespace()
                .filter_map(|v| v.parse().ok())
                .collect();
            if nums.len() == 2 {
                s.pan_x = nums[0].clamp(-1.0, 1.0);
                s.pan_y = nums[1].clamp(-1.0, 1.0);
                s.dirty = true;
                true
            } else {
                eprintln!("pan needs: x y");
                false
            }
        }
        "blur" => match rest.parse::<f32>() {
            Ok(v) => {
                s.blur = v.clamp(0.0, 1.0);
                s.dirty = true;
                true
            }
            Err(_) => false,
        },
        "blur-style" => match rest.parse::<u32>() {
            Ok(v) => {
                s.blur_style = v.min(1);
                s.dirty = true;
                true
            }
            Err(_) => false,
        },
        "model-strength" => {
            let parts: Vec<&str> = rest.split_whitespace().collect();
            match (
                parts.first().and_then(|v| v.parse::<usize>().ok()),
                parts.get(1).and_then(|v| v.parse::<f32>().ok()),
            ) {
                (Some(i), Some(v)) if parts.len() == 2 => {
                    s.model_strengths.push((i, v.clamp(0.0, 1.0)));
                    true
                }
                _ => {
                    eprintln!("model-strength needs: <index> <0.0-1.0>");
                    false
                }
            }
        }
        "background" => {
            s.bg_path = if rest == "off" { None } else { Some(rest.to_string()) };
            s.bg_dirty = true;
            true
        }
        "overlay-opacity" => match rest.parse::<f32>() {
            Ok(v) => {
                s.overlay_opacity = v.clamp(0.0, 1.0);
                s.overlay_dirty = true;
                true
            }
            Err(_) => false,
        },
        _ => {
            eprintln!("unknown control command {cmd:?}");
            false
        }
    };
    if accepted {
        eprintln!("control accepted: {line}");
    }
}
