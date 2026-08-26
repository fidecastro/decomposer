//! Runtime control socket: transport only.
//!
//! The daemon needs to change look without restarting the engine — a restart
//! would tear down /dev/video10, and every application watching it would see
//! the camera disappear mid-call. Lines arrive here and are handed to
//! config::apply_line, the single parser of the protocol; this module knows
//! nothing about what the lines mean.

use anyhow::Result;
use std::io::{BufRead, BufReader};
use std::os::unix::net::UnixListener;

use crate::config::{self, Shared};

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
                    config::apply_line(&line, &state);
                }
            });
        }
    });
    Ok(())
}
