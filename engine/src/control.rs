//! Runtime control socket.
//!
//! The daemon needs to change look without restarting the engine: a restart
//! would tear down /dev/video10, and every application watching it would see
//! the camera disappear mid-call. Instead it sends a line here and the change
//! takes effect on the next frame.
//!
//! Protocol is newline-delimited text, one command per line:
//!   look <name>
//!   strength <0.0-1.0>

use anyhow::Result;
use std::io::{BufRead, BufReader};
use std::os::unix::net::UnixListener;
use std::sync::{Arc, Mutex};

/// What the render loop reads each frame. `dirty` avoids re-uploading the
/// uniform when nothing has changed.
#[derive(Debug, Clone)]
pub struct LookState {
    pub look: u32,
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
}

pub type Shared = Arc<Mutex<LookState>>;

pub fn shared(look: u32, strength: f32, flip: u32) -> Shared {
    Arc::new(Mutex::new(LookState {
        look, strength, flip, dirty: false,
        overlay_path: None,
        overlay_x: 0, overlay_y: 0,
        overlay_max_w: 0, overlay_max_h: 0,
        overlay_opacity: 1.0,
        overlay_dirty: false,
    }))
}

/// Listen on `path`, applying commands to `state`. Runs until the process ends.
pub fn serve(path: String, state: Shared) -> Result<()> {
    // A socket left behind by a killed engine would block bind().
    let _ = std::fs::remove_file(&path);
    let listener = UnixListener::bind(&path)?;
    eprintln!("control {path}");

    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(stream) = stream else { continue };
            let state = state.clone();
            // One thread per client: the daemon holds a long-lived connection,
            // and a CLI poke may arrive alongside it.
            std::thread::spawn(move || {
                let reader = BufReader::new(stream);
                for line in reader.lines() {
                    let Ok(line) = line else { return };
                    apply(&line, &state);
                }
            });
        }
    });
    Ok(())
}

fn apply(line: &str, state: &Shared) {
    let line = line.trim();
    let mut parts = line.splitn(2, char::is_whitespace);
    let Some(cmd) = parts.next() else { return };
    let rest = parts.next().unwrap_or("").trim();
    if rest.is_empty() {
        eprintln!("control command {cmd:?} needs an argument");
        return;
    }
    let Ok(mut s) = state.lock() else { return };
    match cmd {
        "look" => {
            if let Some(idx) = crate::gpu::look_index(rest) {
                s.look = idx;
                s.dirty = true;
            } else {
                eprintln!("unknown look {rest:?}");
            }
        }
        "strength" => {
            if let Ok(v) = rest.parse::<f32>() {
                s.strength = v.clamp(0.0, 1.0);
                s.dirty = true;
            }
        }
        "flip" => {
            if let Ok(v) = rest.parse::<u32>() {
                s.flip = v & 3;
                s.dirty = true;
            }
        }
        "overlay" => {
            // The rest of the line is the path, so filenames may contain spaces.
            s.overlay_path = if rest == "off" { None } else { Some(rest.to_string()) };
            s.overlay_dirty = true;
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
            } else {
                eprintln!("overlay-rect needs: x y max_w max_h");
            }
        }
        "overlay-opacity" => {
            if let Ok(v) = rest.parse::<f32>() {
                s.overlay_opacity = v.clamp(0.0, 1.0);
                s.overlay_dirty = true;
            }
        }
        _ => eprintln!("unknown control command {cmd:?}"),
    }
}
