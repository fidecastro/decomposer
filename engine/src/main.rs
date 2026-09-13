//! decomposer look engine.
//!
//! Reads NV12 frames from the Opal C1 and republishes them to v4l2loopback
//! nodes so applications can choose SEND flips or a stable normal feed.
//!
//! Two input sources, matching decomposer's two modes:
//!   - a V4L2 device (Call mode: the camera's own /dev/video0)
//!   - stdin (Studio mode: raw NV12 piped from the Python depthai layer,
//!     because in that mode the camera has no V4L2 node at all)

use anyhow::{Context, Result};
use clap::Parser;

mod config;
mod control;
mod gpu;
mod lut;
mod overlay;
mod preview;
mod chain;
mod seg;
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

    /// Optional second v4l2loopback node that always publishes normal
    /// orientation. While --flip is set and a viewer is attached to it,
    /// the frame is rendered a second time without the mirror
    #[arg(long)]
    normal_output: Option<String>,

    #[arg(long, default_value_t = 1920)]
    width: u32,

    #[arg(long, default_value_t = 1080)]
    height: u32,

    /// Capture size; 0 means same as the output. Capturing larger than the
    /// output (4K in, 1080p out) is what makes zoom lossless.
    #[arg(long, default_value_t = 0)]
    in_width: u32,

    #[arg(long, default_value_t = 0)]
    in_height: u32,

    /// Digital zoom, 1.0 to 8.0
    #[arg(long, default_value_t = 1.0)]
    zoom: f32,

    /// Crop position across the available margin, -1.0 to 1.0
    #[arg(long, default_value_t = 0.0, allow_negative_numbers = true)]
    pan_x: f32,

    #[arg(long, default_value_t = 0.0, allow_negative_numbers = true)]
    pan_y: f32,

    /// Local contrast (CLAHE) strength, 0.0 to 1.0
    #[arg(long, default_value_t = 0.0)]
    clahe: f32,

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

    /// Background blur strength, 0.0 to 1.0 (0 = off)
    #[arg(long, default_value_t = 0.0)]
    blur: f32,

    /// Blur style: 0 = smooth, 1 = bokeh (highlight-weighted disc)
    #[arg(long, default_value_t = 0)]
    blur_style: u32,

    /// PNG to replace the background with (implies segmentation)
    #[arg(long)]
    background: Option<String>,

    /// Person-segmentation ONNX model; defaults to the bundled MediaPipe
    /// model found next to the engine (models/selfie_segmentation.onnx)
    #[arg(long)]
    seg_model: Option<String>,

    /// Where to run segmentation: cpu or cuda (cuda falls back to cpu)
    #[arg(long, default_value = "cpu")]
    seg_device: String,

    /// Extra model in the chain: path[:cpu|cuda][:strength]. Repeatable;
    /// a one-channel output joins the person mask (strength = weight), a
    /// three-channel output filters the image (strength = blend)
    #[arg(long = "model", action = clap::ArgAction::Append)]
    models: Vec<String>,

    /// Unix socket accepting person masks from an external producer; while
    /// a client is connected the internal model yields
    #[arg(long)]
    mask_sock: Option<String>,

    /// Publish only every Nth frame to the preview
    #[arg(long, default_value_t = 2)]
    preview_every: u64,
}

fn main() -> Result<()> {
    let args = Args::parse();

    let in_w = if args.in_width == 0 { args.width } else { args.in_width };
    let in_h = if args.in_height == 0 { args.height } else { args.in_height };
    let mut source: Box<dyn FrameSource> = if args.input == "-" {
        Box::new(StdinSource::new(in_w, in_h))
    } else {
        Box::new(
            V4l2Source::new(&args.input, in_w, in_h)
                .with_context(|| format!("opening capture device {}", args.input))?,
        )
    };

    let (w, h) = source.dimensions();
    eprintln!("input  {} {}x{} NV12", args.input, w, h);
    let (out_w, out_h) = (args.width, args.height);
    if args.passthrough && (w, h) != (out_w, out_h) {
        anyhow::bail!("passthrough cannot scale: input {w}x{h}, output {out_w}x{out_h}");
    }

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
        let s = source::V4l2Sink::new(&args.output, out_w, out_h)
            .with_context(|| format!("opening output device {}", args.output))?;
        eprintln!("output {} {}x{}", args.output, out_w, out_h);
        Some(s)
    };
    let mut normal_sink = match args.normal_output.as_deref() {
        None => None,
        Some(path) if path == args.output => {
            anyhow::bail!("normal output must differ from primary output {path}")
        }
        // The normal feed is a convenience, the SEND feed is the camera. A
        // node that is busy, missing or not a loopback output must not cost
        // the user the camera, so this one fails soft.
        Some(path) => match source::V4l2Sink::new(path, out_w, out_h) {
            Ok(s) => {
                eprintln!("normal {path} {out_w}x{out_h} (SEND flips removed)");
                Some(s)
            }
            Err(e) => {
                eprintln!("normal {path} unavailable, publishing SEND only: {e:#}");
                None
            }
        },
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

    // File loads (LUT, overlay, background) run off-thread: a slow disk
    // must be a late look, never a frame hitch. Generations guard against
    // a stale load overtaking a newer request.
    enum Loaded {
        Lut(u64, Option<lut::Lut>),
        Overlay(u64, Option<overlay::Overlay>),
        Bg(u64, Option<overlay::Overlay>),
    }
    let (load_tx, load_rx) = std::sync::mpsc::channel::<Loaded>();
    let mut lut_gen = 0u64;
    let mut ov_gen = 0u64;
    let mut bg_gen = 0u64;

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
        let g = gpu::Gpu::new(w, h, out_w, out_h, idx, args.strength, args.flip & 3)?;
        eprintln!("look   {} @ {:.2} on {}", args.look, args.strength, g.adapter_name);
        Some(g)
    };
    let look_state = config::shared(
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
        s.zoom = args.zoom.clamp(1.0, 8.0);
        s.pan_x = args.pan_x.clamp(-1.0, 1.0);
        s.pan_y = args.pan_y.clamp(-1.0, 1.0);
        s.clahe = args.clahe.clamp(0.0, 1.0);
        s.blur = args.blur.clamp(0.0, 1.0);
        s.blur_style = args.blur_style.min(1);
        s.bg_path = args.background.clone();
        s.bg_dirty = s.bg_path.is_some();
        // Force the first pass through the resolver so the initial --look
        // actually loads its LUT instead of silently using a built-in curve.
        s.dirty = true;
    }
    if let Some(path) = args.control.clone() {
        control::serve(path, look_state.clone())?;
    }

    let mut preview = match args.preview.clone() {
        Some(path) => Some(preview::Preview::new(path, args.preview_every, out_w, out_h)?),
        None => None,
    };

    // The model chain: the user's --model entries, plus the person-mask
    // default appended last so user model indexes stay stable. The default
    // is resolved like the LUT dir - an explicit --seg-model wins, the
    // vendored model next to the binary otherwise.
    let mut specs: Vec<chain::Spec> = Vec::new();
    for arg in &args.models {
        specs.push(chain::Spec::parse(arg)?);
    }
    let seg_model = args.seg_model.clone().or_else(|| {
        std::env::current_exe()
            .ok()
            .and_then(|exe| {
                exe.ancestors()
                    .map(|a| a.join("models/selfie_segmentation.onnx"))
                    .find(|p| p.is_file())
            })
            .or_else(|| {
                // The packaged install keeps the model in /usr/share.
                let sys = std::path::PathBuf::from(
                    "/usr/share/decomposer/models/selfie_segmentation.onnx",
                );
                sys.is_file().then_some(sys)
            })
            .map(|p| p.display().to_string())
    });
    let mut model_chain = if engine.is_some() {
        match chain::Chain::start(&specs) {
            Ok(c) => Some(c),
            Err(e) => {
                eprintln!("model chain unavailable: {e:#}");
                None
            }
        }
    } else {
        None
    };
    // A missing default is only an error if something actually needs masks.
    if let Some(c) = model_chain.as_mut() {
        if !c.has_mask_models() {
            if let Some(path) = &seg_model {
                let spec = chain::Spec {
                    path: path.clone(),
                    device: args.seg_device.clone(),
                    strength: 1.0,
                    is_default: true,
                };
                if let Err(e) = c.push(&spec) {
                    eprintln!("default person model unavailable: {e:#}");
                }
            }
        }
    }
    let external_mask: std::sync::Arc<std::sync::Mutex<Option<seg::Mask>>> =
        Default::default();
    if let (Some(c), Some(path)) = (&model_chain, args.mask_sock.clone()) {
        seg::serve_mask_socket(path, external_mask.clone(), c.external.clone())?;
    }
    let mut seg_frame = 0u64;
    let mut layer_live = false;

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
                    (s.look, s.look_name.clone(), s.strength, s.flip,
                     s.zoom, s.pan_x, s.pan_y, s.clahe, s.blur, s.blur_style)
                })
            };
            if let Some((
                look, name, strength, flip, zoom, pan_x, pan_y, clahe, blur, blur_style,
            )) = pending
            {
                g.set_look(look, strength);
                g.set_flip(flip);
                g.set_zoom(zoom, pan_x, pan_y);
                g.set_clahe(clahe);
                g.set_blur(blur, blur_style);
                // Reloading the same LUT every strength tweak would be wasteful,
                // so only touch it when the name actually changes.
                if name != applied_look {
                    applied_look = name.clone();
                    lut_gen += 1;
                    let gen = lut_gen;
                    let dir = lut_dir.clone();
                    let tx = load_tx.clone();
                    std::thread::spawn(move || {
                        let loaded = match (&dir, name.as_str()) {
                            (_, "none") => None,
                            (Some(dir), n) => {
                                lut::find(dir, n).and_then(|p| match lut::load(&p) {
                                    Ok(l) => {
                                        eprintln!("look  {n} from {}", p.display());
                                        Some(l)
                                    }
                                    Err(e) => {
                                        eprintln!("lut: {e:#}");
                                        None
                                    }
                                })
                            }
                            _ => None,
                        };
                        if loaded.is_none()
                            && name != "none"
                            && gpu::look_index(&name).is_none()
                        {
                            eprintln!("unknown look {name:?}");
                        }
                        let _ = tx.send(Loaded::Lut(gen, loaded));
                    });
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
            let bg_change = {
                let mut st = look_state.lock().unwrap();
                st.bg_dirty.then(|| {
                    st.bg_dirty = false;
                    st.bg_path.clone()
                })
            };
            if let Some(change) = bg_change {
                bg_gen += 1;
                let gen = bg_gen;
                match change {
                    // Clearing needs no IO: apply now, and the generation
                    // bump discards any load still in flight.
                    None => g.set_background(None),
                    Some(p) => {
                        let tx = load_tx.clone();
                        std::thread::spawn(move || {
                            let img = match overlay::load(&p, out_w, out_h) {
                                Ok(img) => {
                                    eprintln!(
                                        "background {p} {}x{}",
                                        img.width, img.height
                                    );
                                    Some(img)
                                }
                                Err(e) => {
                                    eprintln!("background: {e:#}");
                                    None
                                }
                            };
                            let _ = tx.send(Loaded::Bg(gen, img));
                        });
                    }
                }
            }

            if let Some((path, ox, oy, mw, mh, opacity)) = overlay_change {
                ov_gen += 1;
                let gen = ov_gen;
                match path {
                    None => g.set_overlay(None, 0, 0, 1.0),
                    Some(p) => {
                        let tx = load_tx.clone();
                        std::thread::spawn(move || {
                            let img = match overlay::load(&p, mw, mh) {
                                Ok(img) => {
                                    eprintln!(
                                        "overlay {p} {}x{} at {ox},{oy}",
                                        img.width, img.height
                                    );
                                    Some(img)
                                }
                                Err(e) => {
                                    eprintln!("overlay: {e:#}");
                                    None
                                }
                            };
                            let _ = tx.send(Loaded::Overlay(gen, img));
                        });
                    }
                }
                let _ = (opacity,); // placement is re-read at apply time
            }

            // Finished loads land between frames; a stale generation means a
            // newer request superseded this one while it was reading disk.
            while let Ok(done) = load_rx.try_recv() {
                match done {
                    Loaded::Lut(gen, loaded) if gen == lut_gen => {
                        g.set_lut(loaded.as_ref());
                    }
                    Loaded::Overlay(gen, img) if gen == ov_gen => {
                        let (ox, oy, opacity) = {
                            let st = look_state.lock().unwrap();
                            (st.overlay_x, st.overlay_y, st.overlay_opacity)
                        };
                        g.set_overlay(img.as_ref(), ox, oy, opacity);
                    }
                    Loaded::Bg(gen, img) if gen == bg_gen => {
                        g.set_background(img.as_ref());
                    }
                    _ => {}
                }
            }
        }
        if let (Some(c), Some(g)) = (model_chain.as_mut(), engine.as_mut()) {
            let (want_blur, want_bg, strength_updates) = {
                let mut st = look_state.lock().unwrap();
                (st.blur > 0.0, st.bg_path.is_some(),
                 std::mem::take(&mut st.model_strengths))
            };
            for (i, v) in strength_updates {
                c.set_strength(i, v);
            }
            c.set_mask_wanted(want_blur || want_bg);
            seg_frame += 1;
            // Every other frame is plenty: model outputs change slower than
            // pixels, and the EMA smooths the seams.
            if c.active() && seg_frame % 2 == 0 {
                c.submit(frame, w, h);
            }
            // An external producer (the mask socket - a user process, or the
            // daemon forwarding the camera's own on-VPU masks) replaces only
            // the bundled default model; user-added mask models keep running
            // on the host and merge in.
            let external = external_mask.lock().unwrap().take();
            let internal = c.take_mask();
            match (external, internal) {
                (Some(a), Some(b)) => {
                    let m = chain::merge_masks(a, b);
                    g.set_mask(&m.data, m.width, m.height);
                }
                (Some(m), None) | (None, Some(m)) => {
                    g.set_mask(&m.data, m.width, m.height);
                }
                (None, None) => {}
            }
            if let Some((layer, lw, lh)) = c.take_layer(frame, w, h) {
                g.set_layer(&layer, lw, lh);
                layer_live = true;
            } else if layer_live && !c.filters_active() {
                g.set_layer(&[], 0, 0);
                layer_live = false;
            }
        }
        // Decided before the GPU runs: the unmirrored pass for the normal
        // feed is only worth doing when that feed will actually be written.
        let normal_wanted = normal_sink.as_mut().is_some_and(|s| s.wants_frame());
        let (out, normal_out): (&[u8], Option<&[u8]>) = match engine.as_mut() {
            Some(g) => {
                g.process(frame)?;
                let normal = if normal_wanted && g.flipped() {
                    g.process_normal()?;
                    Some(g.normal_output())
                } else {
                    None
                };
                (g.output(), normal)
            }
            None => (frame, None),
        };
        if let Some(sink) = sink.as_mut() {
            sink.write_if_watched(out)?;
        }
        if let (Some(sink), true) = (normal_sink.as_mut(), normal_wanted) {
            // Without a SEND flip the two feeds are the same frame.
            sink.write(normal_out.unwrap_or(out))?;
        }
        if let Some(p) = preview.as_mut() {
            p.publish(out, out_w, out_h);
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
