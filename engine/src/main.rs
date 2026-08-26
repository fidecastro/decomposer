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

mod gpu;
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

    let start = std::time::Instant::now();
    let mut engine = if args.passthrough {
        eprintln!("look   passthrough");
        None
    } else {
        let idx = gpu::look_index(&args.look).ok_or_else(|| {
            anyhow::anyhow!("unknown look {:?}. Known: {}", args.look, gpu::LOOKS.join(", "))
        })?;
        let g = gpu::Gpu::new(w, h, idx, args.strength)?;
        eprintln!("look   {} @ {:.2} on {}", args.look, args.strength, g.adapter_name);
        Some(g)
    };

    let mut n: u64 = 0;
    loop {
        let frame = match source.next_frame()? {
            Some(f) => f,
            None => break,
        };
        let out = match engine.as_mut() {
            Some(g) => g.process(frame)?,
            None => frame,
        };
        if let Some(sink) = sink.as_mut() {
            sink.write(out)?;
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
