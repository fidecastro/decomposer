//! decomposer look engine.
//!
//! Reads NV12 frames from the Opal C1 and republishes them to a v4l2loopback
//! node so any application can consume the processed feed.
//!
//! Two input sources, matching decomposer's two modes:
//!   - a V4L2 device (Call mode: the camera's own /dev/video0)
//!   - stdin (Studio mode: raw NV12 piped from the Python depthai layer,
//!     because in that mode the camera has no V4L2 node at all)

use anyhow::{Context, Result};
use clap::Parser;

mod control;
mod gpu;
mod lut;
mod overlay;
mod preview;
mod source;
use source::{FrameSource, StdinSource, V4l2Source};

#[derive(Parser, Debug)]
#[command(name = "decomposer-engine", about = "Capture -> look -> virtual camera")]
struct Args {
    /// Input: a V4L2 device path, or "-" to read raw NV12 from stdin
    #[arg(long, default_value = "/dev/video0")]
    input: String,

    /// v4l2loopback node to publish to
    #[arg(long, default_value = "/dev/video10")]
    output: String,

    #[arg(long, default_value_t = 1920)]
    width: u32,

    #[arg(long, default_value_t = 1080)]
    height: u32,

    /// Stop after N frames (0 = run forever). Useful for smoke tests.
    #[arg(long, default_value_t = 0)]
    frames: u64,

    /// Look to apply
    #[arg(long, default_value = "none")]
    look: String,

    /// How far to push the look, 0.0 to 1.0
    #[arg(long, default_value_t = 1.0)]
    strength: f32,

    /// Skip the GPU entirely and forward frames untouched
    #[arg(long)]
    passthrough: bool,

    /// Unix socket for runtime "look <name>" / "strength <f>" commands
    #[arg(long)]
    control: Option<String>,

    /// Mirror: bit 0 horizontal, bit 1 vertical, 3 = 180 degrees
    #[arg(long, default_value_t = 0)]
    flip: u32,

    /// Directory of .cube LUTs; a look is matched against <name>.cube here
    /// before falling back to the built-in curves
    #[arg(long)]
    lut_dir: Option<String>,

    /// PNG to composite over the frame
    #[arg(long)]
    overlay: Option<String>,

    /// Overlay placement: x,y,max_w,max_h in output pixels (0 = unconstrained)
    #[arg(long, default_value = "0,0,0,0")]
    overlay_rect: String,

    /// Overlay opacity, 0.0 to 1.0
    #[arg(long, default_value_t = 1.0)]
    overlay_opacity: f32,

    /// Unix socket serving a downscaled RGB preview to the control panel
    #[arg(long)]
    preview: Option<String>,

    /// Publish only every Nth frame to the preview
    #[arg(long, default_value_t = 2)]
    preview_every: u64,
}

fn main() -> Result<()> {
    let args = Args::parse();

    let mut source: Box<dyn FrameSource> = if args.input == "-" {
        Box::new(StdinSource::new(args.width, args.height))
    } else {
        Box::new(
            V4l2Source::new(&args.input, args.width, args.height)
                .with_context(|| format!("opening capture device {}", args.input))?,
        )
    };

    let (w, h) = source.dimensions();
    eprintln!("input  {} {}x{} NV12", args.input, w, h);

    // "null" discards frames (benchmarking); "-" writes raw NV12 to stdout,
    // which pipes straight into ffmpeg for inspection.
    let mut stdout_sink = None;
    let mut sink = if args.output == "null" {
        eprintln!("output null (frames discarded)");
        None
    } else if args.output == "-" {
        eprintln!("output stdout (raw NV12)");
        stdout_sink = Some(std::io::stdout().lock());
        None
    } else {
        let s = source::V4l2Sink::new(&args.output, w, h)
            .with_context(|| format!("opening output device {}", args.output))?;
        eprintln!("output {} {}x{}", args.output, w, h);
        Some(s)
    };

    // Timed from the first frame: in Studio mode the producer spends several
    // seconds switching the camera's firmware before anything arrives, and
    // charging that to the frame rate makes the engine look half as fast.
    let mut start = std::time::Instant::now();
    // Resolved before the look is validated, since a LUT name is a valid look
    // even when it is not one of the built-in curves.
    let lut_dir = args.lut_dir.clone().map(std::path::PathBuf::from).or_else(|| {
        std::env::current_exe().ok().and_then(|exe| {
            exe.ancestors().map(|a| a.join("luts")).find(|p| p.is_dir())
        })
    });
    if let Some(d) = &lut_dir {
        eprintln!("luts   {}", d.display());
    }
    let mut applied_look = String::new();

    let mut engine = if args.passthrough {
        eprintln!("look   passthrough");
        None
    } else {
        let has_lut = lut_dir
            .as_ref()
            .map(|d| lut::find(d, &args.look).is_some())
            .unwrap_or(false);
        let idx = match gpu::look_index(&args.look) {
            Some(i) => i,
            None if has_lut => 0,
            None => anyhow::bail!(
                "unknown look {:?}. Built in: {}{}",
                args.look,
                gpu::LOOKS.join(", "),
                lut_dir
                    .as_ref()
                    .map(|d| format!("; LUTs in {}", d.display()))
                    .unwrap_or_default()
            ),
        };
        let g = gpu::Gpu::new(w, h, idx, args.strength, args.flip & 3)?;
        eprintln!("look   {} @ {:.2} on {}", args.look, args.strength, g.adapter_name);
        Some(g)
    };

    let look_state = control::shared(
        gpu::look_index(&args.look).unwrap_or(0),
        args.look.clone(),
        args.strength,
        args.flip & 3,
    );

    {
        let rect: Vec<u32> = args
            .overlay_rect
            .split(',')
            .filter_map(|v| v.trim().parse().ok())
            .collect();
        let mut s = look_state.lock().unwrap();
        s.overlay_path = args.overlay.clone();
        s.overlay_opacity = args.overlay_opacity.clamp(0.0, 1.0);
        if rect.len() == 4 {
            s.overlay_x = rect[0];
            s.overlay_y = rect[1];
            s.overlay_max_w = rect[2];
            s.overlay_max_h = rect[3];
        }
        s.overlay_dirty = s.overlay_path.is_some();
        // Force the first pass through the resolver so the initial --look
        // actually loads its LUT instead of silently using a built-in curve.
        s.dirty = true;
    }
    if let Some(path) = args.control.clone() {
        control::serve(path, look_state.clone())?;
    }

    let mut preview = match args.preview.clone() {
        Some(path) => Some(preview::Preview::new(path, args.preview_every)?),
        None => None,
    };

    let mut n: u64 = 0;
    loop {
        let frame = match source.next_frame()? {
            Some(f) => f,
            None => break,
        };
        if n == 0 {
            start = std::time::Instant::now();
        }
        if let Some(g) = engine.as_mut() {
            // Cheap per-frame check; only touches the GPU when something moved.
            let pending = {
                let mut s = look_state.lock().unwrap();
                s.dirty.then(|| {
                    s.dirty = false;
                    (s.look, s.look_name.clone(), s.strength, s.flip)
                })
            };
            if let Some((look, name, strength, flip)) = pending {
                g.set_look(look, strength);
                g.set_flip(flip);
                // Reloading the same LUT every strength tweak would be wasteful,
                // so only touch it when the name actually changes.
                if name != applied_look {
                    applied_look = name.clone();
                    let loaded = match (&lut_dir, name.as_str()) {
                        (_, "none") => None,
                        (Some(dir), n) => lut::find(dir, n).and_then(|p| match lut::load(&p) {
                            Ok(l) => {
                                eprintln!("look  {n} from {}", p.display());
                                Some(l)
                            }
                            Err(e) => {
                                eprintln!("lut: {e:#}");
                                None
                            }
                        }),
                        _ => None,
                    };
                    match loaded {
                        Some(l) => g.set_lut(Some(&l)),
                        None => {
                            g.set_lut(None);
                            if name != "none" && gpu::look_index(&name).is_none() {
                                eprintln!("unknown look {name:?}");
                            }
                        }
                    }
                }
            }

            // Loading an overlay decodes and rescales a file, so it is handled
            // separately from the cheap uniform updates above.
            let overlay_change = {
                let mut s = look_state.lock().unwrap();
                s.overlay_dirty.then(|| {
                    s.overlay_dirty = false;
                    (
                        s.overlay_path.clone(),
                        s.overlay_x, s.overlay_y,
                        s.overlay_max_w, s.overlay_max_h,
                        s.overlay_opacity,
                    )
                })
            };
            if let Some((path, ox, oy, mw, mh, opacity)) = overlay_change {
                match path {
                    None => g.set_overlay(None, 0, 0, 1.0),
                    Some(p) => match overlay::load(&p, mw, mh) {
                        Ok(img) => {
                            eprintln!("overlay {p} {}x{} at {ox},{oy}", img.width, img.height);
                            g.set_overlay(Some(&img), ox, oy, opacity);
                        }
                        Err(e) => eprintln!("overlay: {e:#}"),
                    },
                }
            }
        }
        let out = match engine.as_mut() {
            Some(g) => g.process(frame)?,
            None => frame,
        };
        if let Some(sink) = sink.as_mut() {
            sink.write(out)?;
        }
        if let Some(p) = preview.as_mut() {
            p.publish(out, w, h);
        }
        if let Some(w) = stdout_sink.as_mut() {
            use std::io::Write;
            w.write_all(out)?;
        }
        n += 1;
        if args.frames != 0 && n >= args.frames {
            break;
        }
    }
    let secs = start.elapsed().as_secs_f64();
    eprintln!("{n} frames in {secs:.1}s ({:.1} fps)", n as f64 / secs);
    Ok(())
}
