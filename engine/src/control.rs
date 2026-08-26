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
#[derive(Debug, Clone, Copy)]
pub struct LookState {
    pub look: u32,
    pub strength: f32,
    pub dirty: bool,
}

pub type Shared = Arc<Mutex<LookState>>;

pub fn shared(look: u32, strength: f32) -> Shared {
    Arc::new(Mutex::new(LookState { look, strength, dirty: false }))
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
    let mut parts = line.split_whitespace();
    let (Some(cmd), Some(arg)) = (parts.next(), parts.next()) else {
        return;
    };
    let Ok(mut s) = state.lock() else { return };
    match cmd {
        "look" => {
            if let Some(idx) = crate::gpu::look_index(arg) {
                s.look = idx;
                s.dirty = true;
            } else {
                eprintln!("unknown look {arg:?}");
            }
        }
        "strength" => {
            if let Ok(v) = arg.parse::<f32>() {
                s.strength = v.clamp(0.0, 1.0);
                s.dirty = true;
            }
        }
        _ => eprintln!("unknown control command {cmd:?}"),
    }
}
