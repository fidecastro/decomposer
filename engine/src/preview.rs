//! Preview feed for the control panel.
//!
//! The panel needs to show what the camera is doing, but it must not open the
//! camera itself: in Studio mode there is no V4L2 node, and even in Call mode a
//! second reader competes with the engine. Since the engine already has every
//! frame, it downscales one and publishes it here.
//!
//! Protocol: on connect the server sends `width: u32, height: u32` little
//! endian, then a continuous stream of RGB24 frames of `width * height * 3`
//! bytes. Slow clients are dropped rather than allowed to stall the pipeline.

use anyhow::Result;
use std::io::Write;
use std::os::unix::net::UnixListener;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TrySendError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub const PREVIEW_W: u32 = 480;

/// A client must take this share of the frames offered to it to count as a
/// panel someone is looking at. A panel reads everything it is sent, at
/// whatever rate that is: 30 / --preview-every, or the 10 a second of a
/// resting camera. The daemon's stall watchdog reads this socket too, one
/// frame every half second whatever it is offered; it must not keep the
/// whole pipeline awake. A fixed rate cannot tell the two apart once the
/// panel's own rate drops near the watchdog's, and the share can.
const LIVE_SHARE: f64 = 0.75;
const LIVE_WINDOW: Duration = Duration::from_secs(1);

struct Client {
    tx: SyncSender<Arc<Vec<u8>>>,
    /// Frames the writer thread has finished sending.
    delivered: Arc<AtomicU64>,
    /// `delivered` at the start of the current rate window.
    seen: u64,
    /// Frames handed to this client this window, taken or skipped.
    offered: u64,
}

type Clients = Arc<Mutex<Vec<Client>>>;

pub struct Preview {
    clients: Clients,
    /// Only every Nth frame is published: the panel does not need 30fps.
    every: u64,
    counter: u64,
    scratch: Vec<u8>,
    /// Preview dimensions follow the output's aspect ratio: squeezing a
    /// 4:3 frame into a fixed 16:9 preview is a distortion, not a preview.
    pw: u32,
    ph: u32,
    window_start: Instant,
    live: bool,
}

impl Preview {
    pub fn new(path: String, every: u64, out_w: u32, out_h: u32) -> Result<Self> {
        let pw = PREVIEW_W;
        let ph = ((PREVIEW_W as u64 * out_h as u64 / out_w.max(1) as u64) as u32)
            .max(2)
            & !1;
        let clients: Clients = Arc::new(Mutex::new(Vec::new()));
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path)?;
        eprintln!("preview {path} {pw}x{ph} rgb24");

        let accept_clients = clients.clone();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                // A depth of 2 keeps latency low; a stalled panel just misses frames.
                let (tx, rx): (SyncSender<Arc<Vec<u8>>>, Receiver<Arc<Vec<u8>>>) =
                    sync_channel(2);
                let mut header = Vec::with_capacity(8);
                header.extend_from_slice(&pw.to_le_bytes());
                header.extend_from_slice(&ph.to_le_bytes());
                if stream.write_all(&header).is_err() {
                    continue;
                }
                let delivered = Arc::new(AtomicU64::new(0));
                accept_clients.lock().unwrap().push(Client {
                    tx,
                    delivered: delivered.clone(),
                    seen: 0,
                    offered: 0,
                });
                std::thread::spawn(move || {
                    for frame in rx {
                        if stream.write_all(&frame).is_err() {
                            return; // panel closed
                        }
                        delivered.fetch_add(1, Ordering::Relaxed);
                    }
                });
            }
        });

        Ok(Self {
            clients,
            every: every.max(1),
            counter: 0,
            scratch: vec![0u8; (pw * ph * 3) as usize],
            pw,
            ph,
            window_start: Instant::now(),
            live: false,
        })
    }

    pub fn has_clients(&self) -> bool {
        self.clients.lock().map(|c| !c.is_empty()).unwrap_or(false)
    }

    /// Whether some client drains frames the way an open panel does.
    /// Measured over one-second windows, so a panel that just connected is
    /// live once it has kept up with the keep-warm frames for a second.
    pub fn live(&mut self) -> bool {
        let elapsed = self.window_start.elapsed();
        if elapsed >= LIVE_WINDOW {
            let mut live = false;
            if let Ok(mut clients) = self.clients.lock() {
                for c in clients.iter_mut() {
                    let delivered = c.delivered.load(Ordering::Relaxed);
                    live |= drains_live(delivered - c.seen, c.offered);
                    c.seen = delivered;
                    c.offered = 0;
                }
            }
            self.live = live;
            self.window_start = Instant::now();
        }
        self.live
    }

    /// Advance the frame counter: true when a live panel should get this
    /// frame (every Nth).
    pub fn wants_frame(&mut self) -> bool {
        self.counter += 1;
        self.live() && self.counter % self.every == 0
    }

    /// Downscale one NV12 frame and hand it to every connected client.
    pub fn publish(&mut self, nv12: &[u8], width: u32, height: u32) {
        if !self.has_clients() {
            return;
        }
        downscale_nv12_to_rgb(nv12, width, height, self.pw, self.ph, &mut self.scratch);
        let frame = Arc::new(self.scratch.clone());

        let mut clients = match self.clients.lock() {
            Ok(c) => c,
            Err(_) => return,
        };
        clients.retain_mut(|c| {
            c.offered += 1;
            match c.tx.try_send(frame.clone()) {
                Ok(()) => true,
                // Full means the panel is behind: skip this frame, keep the client.
                Err(TrySendError::Full(_)) => true,
                Err(TrySendError::Disconnected(_)) => false,
            }
        });
    }
}

/// Whether a client that finished `delivered` of the `offered` frames reads
/// like an open panel rather than the watchdog's occasional sample.
fn drains_live(delivered: u64, offered: u64) -> bool {
    offered > 0 && delivered as f64 >= offered as f64 * LIVE_SHARE
}

/// Nearest-neighbour NV12 to RGB24. The output is a few hundred pixels wide, so
/// sampling beats filtering for both cost and clarity.
fn downscale_nv12_to_rgb(nv12: &[u8], sw: u32, sh: u32, pw: u32, ph: u32, out: &mut [u8]) {
    let uv_base = (sw * sh) as usize;
    for dy in 0..ph {
        let sy = dy * sh / ph;
        for dx in 0..pw {
            let sx = dx * sw / pw;

            let y_idx = (sy * sw + sx) as usize;
            let uv_idx = uv_base + ((sy / 2) * sw + (sx & !1)) as usize;
            if y_idx >= nv12.len() || uv_idx + 1 >= nv12.len() {
                continue;
            }

            // BT.601 limited range, matching the shader.
            let y = (nv12[y_idx] as f32 - 16.0) * (255.0 / 219.0);
            let u = nv12[uv_idx] as f32 - 128.0;
            let v = nv12[uv_idx + 1] as f32 - 128.0;
            let (u, v) = (u * (255.0 / 224.0), v * (255.0 / 224.0));

            let o = ((dy * pw + dx) * 3) as usize;
            out[o] = (y + 1.402 * v).clamp(0.0, 255.0) as u8;
            out[o + 1] = (y - 0.344136 * u - 0.714136 * v).clamp(0.0, 255.0) as u8;
            out[o + 2] = (y + 1.772 * u).clamp(0.0, 255.0) as u8;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::drains_live;

    #[test]
    fn the_stall_watchdog_is_not_a_panel() {
        // Two frames a second, of the keep-warm ten or a full-rate thirty.
        assert!(!drains_live(2, 10));
        assert!(!drains_live(2, 30));
    }

    #[test]
    fn a_panel_reading_every_frame_is_live() {
        // Keep-warm frames while the camera rests, and full rate after.
        assert!(drains_live(10, 10));
        assert!(drains_live(15, 15));
    }

    #[test]
    fn a_slow_preview_is_still_live() {
        // --preview-every 8 at 30 fps, and a 5 fps capture mode.
        assert!(drains_live(4, 4));
        assert!(drains_live(5, 5));
    }

    #[test]
    fn a_panel_may_skip_a_frame() {
        assert!(drains_live(9, 10));
        assert!(!drains_live(7, 10));
    }

    #[test]
    fn nothing_offered_is_not_live() {
        assert!(!drains_live(0, 0));
    }
}
