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
use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TrySendError};
use std::sync::{Arc, Mutex};

pub const PREVIEW_W: u32 = 480;
pub const PREVIEW_H: u32 = 270;

type Clients = Arc<Mutex<Vec<SyncSender<Arc<Vec<u8>>>>>>;

pub struct Preview {
    clients: Clients,
    /// Only every Nth frame is published: the panel does not need 30fps.
    every: u64,
    counter: u64,
    scratch: Vec<u8>,
}

impl Preview {
    pub fn new(path: String, every: u64) -> Result<Self> {
        let clients: Clients = Arc::new(Mutex::new(Vec::new()));
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path)?;
        eprintln!("preview {path} {PREVIEW_W}x{PREVIEW_H} rgb24");

        let accept_clients = clients.clone();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                // A depth of 2 keeps latency low; a stalled panel just misses frames.
                let (tx, rx): (SyncSender<Arc<Vec<u8>>>, Receiver<Arc<Vec<u8>>>) =
                    sync_channel(2);
                let mut header = Vec::with_capacity(8);
                header.extend_from_slice(&PREVIEW_W.to_le_bytes());
                header.extend_from_slice(&PREVIEW_H.to_le_bytes());
                if stream.write_all(&header).is_err() {
                    continue;
                }
                accept_clients.lock().unwrap().push(tx);
                std::thread::spawn(move || {
                    for frame in rx {
                        if stream.write_all(&frame).is_err() {
                            return; // panel closed
                        }
                    }
                });
            }
        });

        Ok(Self {
            clients,
            every: every.max(1),
            counter: 0,
            scratch: vec![0u8; (PREVIEW_W * PREVIEW_H * 3) as usize],
        })
    }

    pub fn has_clients(&self) -> bool {
        self.clients.lock().map(|c| !c.is_empty()).unwrap_or(false)
    }

    /// Downscale one NV12 frame and hand it to every connected panel.
    pub fn publish(&mut self, nv12: &[u8], width: u32, height: u32) {
        self.counter += 1;
        if self.counter % self.every != 0 || !self.has_clients() {
            return;
        }
        downscale_nv12_to_rgb(nv12, width, height, &mut self.scratch);
        let frame = Arc::new(self.scratch.clone());

        let mut clients = match self.clients.lock() {
            Ok(c) => c,
            Err(_) => return,
        };
        clients.retain(|tx| match tx.try_send(frame.clone()) {
            Ok(()) => true,
            // Full means the panel is behind: skip this frame, keep the client.
            Err(TrySendError::Full(_)) => true,
            Err(TrySendError::Disconnected(_)) => false,
        });
    }
}

/// Nearest-neighbour NV12 to RGB24. The output is a few hundred pixels wide, so
/// sampling beats filtering for both cost and clarity.
fn downscale_nv12_to_rgb(nv12: &[u8], sw: u32, sh: u32, out: &mut [u8]) {
    let uv_base = (sw * sh) as usize;
    for dy in 0..PREVIEW_H {
        let sy = dy * sh / PREVIEW_H;
        for dx in 0..PREVIEW_W {
            let sx = dx * sw / PREVIEW_W;

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

            let o = ((dy * PREVIEW_W + dx) * 3) as usize;
            out[o] = (y + 1.402 * v).clamp(0.0, 255.0) as u8;
            out[o + 1] = (y - 0.344136 * u - 0.714136 * v).clamp(0.0, 255.0) as u8;
            out[o + 2] = (y + 1.772 * u).clamp(0.0, 255.0) as u8;
        }
    }
}
