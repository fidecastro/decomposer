//! GPU look pipeline.
//!
//! One compute dispatch per frame: NV12 uploaded as a storage buffer, graded,
//! and written straight back out as NV12. Keeping both ends in the camera's
//! native format avoids two format conversions on the CPU, which is what made
//! the original NumPy implementation unable to hold 30 fps.

use anyhow::{anyhow, Result};
use wgpu::util::DeviceExt;

pub const LOOKS: &[&str] = &[
    "none", "process", "chrome", "fade", "instant", "mono", "noir", "tonal", "transfer",
];

pub fn look_index(name: &str) -> Option<u32> {
    LOOKS.iter().position(|l| *l == name).map(|i| i as u32)
}

#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
struct Params {
    width: u32,
    height: u32,
    look: u32,
    strength: f32,
    /// bit 0: mirror horizontally, bit 1: mirror vertically.
    flip: u32,
    /// Overlay placement in output pixels; ov_w == 0 disables it.
    ov_x: u32,
    ov_y: u32,
    ov_w: u32,
    ov_h: u32,
    ov_opacity: f32,
    /// Edge length of the loaded LUT; 0 means fall back to the built-in look.
    lut_size: u32,
    /// Source dimensions; may exceed the output (that headroom is the zoom).
    src_w: u32,
    src_h: u32,
    zoom: f32,
    pan_x: f32,
    pan_y: f32,
    /// CLAHE strength (0 = off) and clip limit.
    clahe: f32,
    clahe_clip: f32,
    /// Background blur strength; mask_w == 0 means no mask yet, which
    /// disables both blur and replacement regardless of the settings.
    blur: f32,
    mask_w: u32,
    mask_h: u32,
    /// Background image size; bg_w == 0 means blur, not replace.
    bg_w: u32,
    bg_h: u32,
    /// Filter-layer residual size; layer_w == 0 means no filters active.
    layer_w: u32,
    layer_h: u32,
    /// 0 = smooth blur, 1 = bokeh (highlight-weighted disc).
    blur_style: u32,
    // WGSL rounds uniform structs up to 16 bytes; pad explicitly so the Rust
    // and shader layouts cannot silently disagree.
    _pad: [u32; 2],
}

pub struct Gpu {
    device: wgpu::Device,
    queue: wgpu::Queue,
    pipeline: wgpu::ComputePipeline,
    hist_pipeline: wgpu::ComputePipeline,
    cdf_pipeline: wgpu::ComputePipeline,
    bind_group: wgpu::BindGroup,
    layout: wgpu::BindGroupLayout,
    params_buf: wgpu::Buffer,
    src_buf: wgpu::Buffer,
    dst_buf: wgpu::Buffer,
    overlay_buf: wgpu::Buffer,
    lut_buf: wgpu::Buffer,
    clahe_hist: wgpu::Buffer,
    clahe_lut: wgpu::Buffer,
    mask_buf: wgpu::Buffer,
    bg_buf: wgpu::Buffer,
    layer_buf: wgpu::Buffer,
    staging: wgpu::Buffer,
    params: Params,
    size: u64,
    src_size: u64,
    /// The SEND frame, and the same frame rendered without the mirror for
    /// the normal feed. Two buffers so both stay readable at once.
    out: Vec<u8>,
    normal_out: Vec<u8>,
    pub adapter_name: String,
}

impl Gpu {
    pub fn new(
        src_w: u32, src_h: u32,
        width: u32, height: u32,
        look: u32, strength: f32, flip: u32,
    ) -> Result<Self> {
        let instance = wgpu::Instance::default();
        let adapter = pollster::block_on(
            instance.request_adapter(&wgpu::RequestAdapterOptions {
                power_preference: wgpu::PowerPreference::HighPerformance,
                ..Default::default()
            }),
        )
        .map_err(|e| anyhow!("no suitable GPU adapter: {e}"))?;
        let adapter_name = adapter.get_info().name;

        let (device, queue) =
            pollster::block_on(adapter.request_device(&wgpu::DeviceDescriptor {
                label: Some("decomposer"),
                // The default limits are the WebGPU browser baseline (8
                // storage buffers per stage); we bind 9. This is a native
                // app: ask for what the hardware actually has.
                required_limits: adapter.limits(),
                ..Default::default()
            }))
            .map_err(|e| anyhow!("could not create GPU device: {e}"))?;

        let src_size = super::source::nv12_len(src_w, src_h) as u64;
        let size = super::source::nv12_len(width, height) as u64;
        let params = Params {
            width, height, look, strength, flip,
            ov_x: 0, ov_y: 0, ov_w: 0, ov_h: 0, ov_opacity: 1.0,
            lut_size: 0,
            src_w, src_h,
            zoom: 1.0, pan_x: 0.0, pan_y: 0.0,
            clahe: 0.0, clahe_clip: 2.5,
            blur: 0.0, mask_w: 0, mask_h: 0, bg_w: 0, bg_h: 0,
            layer_w: 0, layer_h: 0,
            blur_style: 0,
            _pad: [0; 2],
        };

        let params_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("params"),
            contents: bytemuck::bytes_of(&params),
            usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST,
        });
        let mk = |label, usage| {
            device.create_buffer(&wgpu::BufferDescriptor {
                label: Some(label),
                size,
                usage,
                mapped_at_creation: false,
            })
        };
        let src_buf = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("src"),
            size: src_size,
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });
        let dst_buf = mk("dst", wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC);
        let staging = mk("staging", wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST);
        // A storage binding cannot be empty, so an absent overlay is a single
        // transparent pixel that the shader never reads (ov_w stays 0).
        let overlay_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("overlay"),
            contents: bytemuck::cast_slice(&[0u32]),
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
        });
        let lut_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("lut"),
            contents: bytemuck::cast_slice(&[0f32; 4]),
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
        });
        // Placeholders: a storage binding cannot be empty. mask_w/bg_w of 0
        // keep the shader from ever reading them.
        let mask_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("mask"),
            contents: bytemuck::cast_slice(&[0u32]),
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
        });
        let bg_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("background"),
            contents: bytemuck::cast_slice(&[0u32]),
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
        });
        let layer_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("filter-layer"),
            contents: bytemuck::cast_slice(&[0u32]),
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
        });
        // 8x8 tiles x 256 bins. WebGPU zero-initializes, which is exactly the
        // state the histogram pass wants to start from.
        let clahe_size = (8 * 8 * 256 * 4) as u64;
        let clahe_hist = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("clahe-hist"),
            size: clahe_size,
            usage: wgpu::BufferUsages::STORAGE,
            mapped_at_creation: false,
        });
        let clahe_lut = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("clahe-lut"),
            size: clahe_size,
            usage: wgpu::BufferUsages::STORAGE,
            mapped_at_creation: false,
        });

        let shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("look"),
            source: wgpu::ShaderSource::Wgsl(include_str!("../shaders/look.wgsl").into()),
        });

        let storage = |read_only| wgpu::BindingType::Buffer {
            ty: wgpu::BufferBindingType::Storage { read_only },
            has_dynamic_offset: false,
            min_binding_size: None,
        };
        let layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
            label: Some("look-layout"),
            entries: &[
                wgpu::BindGroupLayoutEntry {
                    binding: 0,
                    visibility: wgpu::ShaderStages::COMPUTE,
                    ty: wgpu::BindingType::Buffer {
                        ty: wgpu::BufferBindingType::Uniform,
                        has_dynamic_offset: false,
                        min_binding_size: None,
                    },
                    count: None,
                },
                wgpu::BindGroupLayoutEntry {
                    binding: 1,
                    visibility: wgpu::ShaderStages::COMPUTE,
                    ty: storage(true),
                    count: None,
                },
                wgpu::BindGroupLayoutEntry {
                    binding: 2,
                    visibility: wgpu::ShaderStages::COMPUTE,
                    ty: storage(false),
                    count: None,
                },
                wgpu::BindGroupLayoutEntry {
                    binding: 3,
                    visibility: wgpu::ShaderStages::COMPUTE,
                    ty: storage(true),
                    count: None,
                },
                wgpu::BindGroupLayoutEntry {
                    binding: 4,
                    visibility: wgpu::ShaderStages::COMPUTE,
                    ty: storage(true),
                    count: None,
                },
                wgpu::BindGroupLayoutEntry {
                    binding: 5,
                    visibility: wgpu::ShaderStages::COMPUTE,
                    ty: storage(false),
                    count: None,
                },
                wgpu::BindGroupLayoutEntry {
                    binding: 6,
                    visibility: wgpu::ShaderStages::COMPUTE,
                    ty: storage(false),
                    count: None,
                },
                wgpu::BindGroupLayoutEntry {
                    binding: 7,
                    visibility: wgpu::ShaderStages::COMPUTE,
                    ty: storage(true),
                    count: None,
                },
                wgpu::BindGroupLayoutEntry {
                    binding: 8,
                    visibility: wgpu::ShaderStages::COMPUTE,
                    ty: storage(true),
                    count: None,
                },
                wgpu::BindGroupLayoutEntry {
                    binding: 9,
                    visibility: wgpu::ShaderStages::COMPUTE,
                    ty: storage(true),
                    count: None,
                },
            ],
        });

        let bind_group = make_bind_group(
            &device, &layout, &params_buf, &src_buf, &dst_buf, &overlay_buf,
            &lut_buf, &clahe_hist, &clahe_lut, &mask_buf, &bg_buf, &layer_buf,
        );

        let pipeline_layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
            label: Some("look-pipeline-layout"),
            bind_group_layouts: &[Some(&layout)],
            immediate_size: 0,
        });
        let mk_pipeline = |label: &str, entry: &str| {
            device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
                label: Some(label),
                layout: Some(&pipeline_layout),
                module: &shader,
                entry_point: Some(entry),
                compilation_options: Default::default(),
                cache: None,
            })
        };
        let pipeline = mk_pipeline("look-pipeline", "main");
        let hist_pipeline = mk_pipeline("clahe-hist", "clahe_hist_main");
        let cdf_pipeline = mk_pipeline("clahe-cdf", "clahe_cdf_main");

        Ok(Self {
            device, queue, pipeline, hist_pipeline, cdf_pipeline,
            bind_group, layout, params_buf, src_buf, dst_buf,
            overlay_buf, lut_buf, clahe_hist, clahe_lut,
            mask_buf, bg_buf, layer_buf,
            staging, params, size, src_size,
            out: vec![0u8; size as usize],
            normal_out: vec![0u8; size as usize],
            adapter_name,
        })
    }

    /// Change the look without rebuilding the pipeline. Used by the daemon
    /// when the user switches look at runtime.
    pub fn set_look(&mut self, look: u32, strength: f32) {
        self.params.look = look;
        self.params.strength = strength;
        self.upload_params();
    }

    /// Install a colour lookup table, or clear it with `None` to fall back to
    /// the built-in curves.
    pub fn set_lut(&mut self, lut: Option<&crate::lut::Lut>) {
        match lut {
            None => self.params.lut_size = 0,
            Some(l) => {
                let needed = (l.data.len() * 16) as u64;
                if self.lut_buf.size() < needed {
                    self.lut_buf = self.device.create_buffer(&wgpu::BufferDescriptor {
                        label: Some("lut"),
                        size: needed,
                        usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
                        mapped_at_creation: false,
                    });
                    self.bind_group = make_bind_group(
                        &self.device, &self.layout, &self.params_buf,
                        &self.src_buf, &self.dst_buf, &self.overlay_buf,
                        &self.lut_buf, &self.clahe_hist, &self.clahe_lut,
                        &self.mask_buf, &self.bg_buf, &self.layer_buf,
                    );
                }
                self.queue.write_buffer(&self.lut_buf, 0, bytemuck::cast_slice(&l.data));
                self.params.lut_size = l.size;
            }
        }
        self.upload_params();
    }

    /// Place an image over the frame, or clear it with `None`.
    ///
    /// The buffer is rebuilt only when the overlay's size changes; moving it or
    /// fading it just rewrites the uniform.
    pub fn set_overlay(
        &mut self,
        overlay: Option<&crate::overlay::Overlay>,
        x: u32,
        y: u32,
        opacity: f32,
    ) {
        match overlay {
            None => {
                self.params.ov_w = 0;
                self.params.ov_h = 0;
            }
            Some(ov) if ov.is_empty() => {
                self.params.ov_w = 0;
                self.params.ov_h = 0;
            }
            Some(ov) => {
                let needed = (ov.pixels.len() * 4) as u64;
                if self.overlay_buf.size() < needed {
                    self.overlay_buf = self.device.create_buffer(&wgpu::BufferDescriptor {
                        label: Some("overlay"),
                        size: needed,
                        usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
                        mapped_at_creation: false,
                    });
                    self.bind_group = make_bind_group(
                        &self.device, &self.layout, &self.params_buf,
                        &self.src_buf, &self.dst_buf, &self.overlay_buf,
                        &self.lut_buf, &self.clahe_hist, &self.clahe_lut,
                        &self.mask_buf, &self.bg_buf, &self.layer_buf,
                    );
                }
                self.queue.write_buffer(
                    &self.overlay_buf, 0, bytemuck::cast_slice(&ov.pixels),
                );
                self.params.ov_w = ov.width;
                self.params.ov_h = ov.height;
            }
        }
        self.params.ov_x = x;
        self.params.ov_y = y;
        self.params.ov_opacity = opacity.clamp(0.0, 1.0);
        self.upload_params();
    }

    /// Mirror the image. Costs nothing: it only changes where the shader reads.
    pub fn set_flip(&mut self, flip: u32) {
        self.params.flip = flip;
        self.upload_params();
    }

    /// Local contrast (CLAHE). 0 disables and skips the extra passes.
    pub fn set_clahe(&mut self, strength: f32) {
        self.params.clahe = strength.clamp(0.0, 1.0);
        self.upload_params();
    }

    /// Digital zoom and pan, applied in the same coordinate mapping as the
    /// mirror. Lossless up to src/out ratio; upscaling beyond.
    pub fn set_zoom(&mut self, zoom: f32, pan_x: f32, pan_y: f32) {
        self.params.zoom = zoom.clamp(1.0, 8.0);
        self.params.pan_x = pan_x.clamp(-1.0, 1.0);
        self.params.pan_y = pan_y.clamp(-1.0, 1.0);
        self.upload_params();
    }

    /// Background blur strength. The heavy work only happens in the shader
    /// when both blur > 0 and a mask has actually arrived.
    pub fn set_blur(&mut self, blur: f32, style: u32) {
        self.params.blur = blur.clamp(0.0, 1.0);
        self.params.blur_style = style.min(1);
        self.upload_params();
    }

    /// Install a fresh person mask (u8, source-frame space).
    pub fn set_mask(&mut self, mask: &[u8], w: u32, h: u32) {
        let needed = (mask.len().div_ceil(4) * 4) as u64;
        if self.mask_buf.size() < needed {
            self.mask_buf = self.device.create_buffer(&wgpu::BufferDescriptor {
                label: Some("mask"),
                size: needed,
                usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
                mapped_at_creation: false,
            });
            self.rebind();
        }
        let mut padded = mask.to_vec();
        padded.resize(needed as usize, 0);
        self.queue.write_buffer(&self.mask_buf, 0, &padded);
        self.params.mask_w = w;
        self.params.mask_h = h;
        self.upload_params();
    }

    /// Replace the background with an image (pre-scaled to the output size),
    /// or clear it with `None` to go back to blurring.
    pub fn set_background(&mut self, bg: Option<&crate::overlay::Overlay>) {
        match bg {
            None => {
                self.params.bg_w = 0;
                self.params.bg_h = 0;
            }
            Some(img) if img.is_empty() => {
                self.params.bg_w = 0;
                self.params.bg_h = 0;
            }
            Some(img) => {
                let needed = (img.pixels.len() * 4) as u64;
                if self.bg_buf.size() < needed {
                    self.bg_buf = self.device.create_buffer(&wgpu::BufferDescriptor {
                        label: Some("background"),
                        size: needed,
                        usage: wgpu::BufferUsages::STORAGE
                            | wgpu::BufferUsages::COPY_DST,
                        mapped_at_creation: false,
                    });
                    self.rebind();
                }
                self.queue
                    .write_buffer(&self.bg_buf, 0, bytemuck::cast_slice(&img.pixels));
                self.params.bg_w = img.width;
                self.params.bg_h = img.height;
            }
        }
        self.upload_params();
    }

    /// Install a filter-layer residual (RGB u8, biased at 128, source
    /// space), or clear it with an empty slice.
    pub fn set_layer(&mut self, layer: &[u8], w: u32, h: u32) {
        if layer.is_empty() {
            self.params.layer_w = 0;
            self.params.layer_h = 0;
            self.upload_params();
            return;
        }
        let needed = (layer.len().div_ceil(4) * 4) as u64;
        if self.layer_buf.size() < needed {
            self.layer_buf = self.device.create_buffer(&wgpu::BufferDescriptor {
                label: Some("filter-layer"),
                size: needed,
                usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
                mapped_at_creation: false,
            });
            self.rebind();
        }
        let mut padded = layer.to_vec();
        padded.resize(needed as usize, 0);
        self.queue.write_buffer(&self.layer_buf, 0, &padded);
        self.params.layer_w = w;
        self.params.layer_h = h;
        self.upload_params();
    }

    fn rebind(&mut self) {
        self.bind_group = make_bind_group(
            &self.device, &self.layout, &self.params_buf,
            &self.src_buf, &self.dst_buf, &self.overlay_buf,
            &self.lut_buf, &self.clahe_hist, &self.clahe_lut,
            &self.mask_buf, &self.bg_buf, &self.layer_buf,
        );
    }

    fn upload_params(&mut self) {
        self.queue
            .write_buffer(&self.params_buf, 0, bytemuck::bytes_of(&self.params));
    }

    /// Whether the SEND output is mirrored on either axis.
    pub fn flipped(&self) -> bool {
        self.params.flip != 0
    }

    /// Grade one NV12 frame into the SEND output; read it with `output()`.
    pub fn process(&mut self, frame: &[u8]) -> Result<()> {
        let n = (frame.len() as u64).min(self.src_size);
        self.queue.write_buffer(&self.src_buf, 0, &frame[..n as usize]);
        self.render(false)
    }

    /// Grade the frame last given to `process` once more with no mirror,
    /// into the buffer `normal_output()` reads.
    ///
    /// The mirror is applied where the source is sampled, while the overlay
    /// and a replacement background are placed in output pixels. Undoing
    /// the mirror on the finished frame would therefore mirror those too,
    /// so the normal feed is a second pass rather than a copy. Only the
    /// uniform changes; the source is already on the GPU.
    pub fn process_normal(&mut self) -> Result<()> {
        let send_flip = self.params.flip;
        self.params.flip = 0;
        self.upload_params();
        let rendered = self.render(true);
        self.params.flip = send_flip;
        self.upload_params();
        rendered
    }

    /// The SEND frame from the last `process` call.
    pub fn output(&self) -> &[u8] {
        &self.out
    }

    /// The unmirrored frame from the last `process_normal` call.
    pub fn normal_output(&self) -> &[u8] {
        &self.normal_out
    }

    /// Run the passes over the uploaded source with the current uniform,
    /// then read the result back into one of the two output buffers.
    fn render(&mut self, into_normal: bool) -> Result<()> {
        let mut enc = self
            .device
            .create_command_encoder(&wgpu::CommandEncoderDescriptor { label: Some("look") });
        {
            let mut pass = enc.begin_compute_pass(&wgpu::ComputePassDescriptor {
                label: Some("look-pass"),
                timestamp_writes: None,
            });
            pass.set_bind_group(0, &self.bind_group, &[]);
            if self.params.clahe > 0.0 {
                // Dispatches within one pass are ordered with visibility, so
                // hist -> cdf -> main needs no explicit barriers.
                pass.set_pipeline(&self.hist_pipeline);
                pass.dispatch_workgroups(
                    self.params.width.div_ceil(16),
                    self.params.height.div_ceil(16),
                    1,
                );
                pass.set_pipeline(&self.cdf_pipeline);
                pass.dispatch_workgroups(64, 1, 1);
            }
            pass.set_pipeline(&self.pipeline);
            // One invocation per 4x2 pixel block.
            let gx = self.params.width.div_ceil(4).div_ceil(8);
            let gy = self.params.height.div_ceil(2).div_ceil(8);
            pass.dispatch_workgroups(gx, gy, 1);
        }
        enc.copy_buffer_to_buffer(&self.dst_buf, 0, &self.staging, 0, self.size);
        self.queue.submit(Some(enc.finish()));

        let slice = self.staging.slice(..);
        let (tx, rx) = std::sync::mpsc::channel();
        slice.map_async(wgpu::MapMode::Read, move |r| {
            let _ = tx.send(r);
        });
        self.device.poll(wgpu::PollType::wait_indefinitely())?;
        rx.recv()??;

        {
            let view = slice.get_mapped_range()?;
            let target = if into_normal { &mut self.normal_out } else { &mut self.out };
            target.copy_from_slice(&view[..]);
        }
        self.staging.unmap();
        Ok(())
    }
}

#[allow(clippy::too_many_arguments)]
fn make_bind_group(
    device: &wgpu::Device,
    layout: &wgpu::BindGroupLayout,
    params: &wgpu::Buffer,
    src: &wgpu::Buffer,
    dst: &wgpu::Buffer,
    overlay: &wgpu::Buffer,
    lut: &wgpu::Buffer,
    clahe_hist: &wgpu::Buffer,
    clahe_lut: &wgpu::Buffer,
    mask: &wgpu::Buffer,
    bg: &wgpu::Buffer,
    layer: &wgpu::Buffer,
) -> wgpu::BindGroup {
    device.create_bind_group(&wgpu::BindGroupDescriptor {
        label: Some("look-bind"),
        layout,
        entries: &[
            wgpu::BindGroupEntry { binding: 0, resource: params.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 1, resource: src.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 2, resource: dst.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 3, resource: overlay.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 4, resource: lut.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 5, resource: clahe_hist.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 6, resource: clahe_lut.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 7, resource: mask.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 8, resource: bg.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 9, resource: layer.as_entire_binding() },
        ],
    })
}

#[cfg(test)]
mod tests {
    //! These need a GPU adapter. Where wgpu finds none the tests report it
    //! and pass, so a headless CI box does not fail on hardware it lacks.
    use super::Gpu;
    use crate::overlay::Overlay;

    const W: u32 = 64;
    const H: u32 = 32;

    /// A dark left half and a bright right half, neutral chroma: the
    /// orientation of the result is readable from a single luma sample.
    fn split_frame() -> Vec<u8> {
        let mut f = vec![128u8; crate::source::nv12_len(W, H)];
        for y in 0..H {
            for x in 0..W {
                f[(y * W + x) as usize] = if x < W / 2 { 50 } else { 200 };
            }
        }
        f
    }

    fn luma(nv12: &[u8], x: u32, y: u32) -> u8 {
        nv12[(y * W + x) as usize]
    }

    fn near(a: u8, b: u8) -> bool {
        (a as i32 - b as i32).abs() <= 3
    }

    fn solid(width: u32, height: u32, rgba: u32) -> Overlay {
        Overlay { width, height, pixels: vec![rgba; (width * height) as usize] }
    }

    fn gpu(flip: u32) -> Option<Gpu> {
        match Gpu::new(W, H, W, H, 0, 1.0, flip) {
            Ok(g) => Some(g),
            Err(e) => {
                eprintln!("skipping GPU test: {e:#}");
                None
            }
        }
    }

    // Opaque red, as the shader writes it: Y' ~ 82 in limited range.
    const RED: u32 = 0xff00_00ff;
    const RED_LUMA: u8 = 82;

    #[test]
    fn normal_pass_unmirrors_the_image_but_not_the_overlay() {
        let Some(mut g) = gpu(1) else { return };
        g.set_overlay(Some(&solid(4, 4, RED)), 2, 2, 1.0);
        g.process(&split_frame()).unwrap();
        g.process_normal().unwrap();

        // SEND: mirrored, so the bright half is on the left.
        let send = g.output();
        assert!(near(luma(send, 10, 20), 200), "send left {}", luma(send, 10, 20));
        assert!(near(luma(send, 50, 20), 50), "send right {}", luma(send, 50, 20));
        assert!(near(luma(send, 3, 3), RED_LUMA), "send overlay {}", luma(send, 3, 3));

        // Normal: the camera image is upright again, the logo has not moved.
        let normal = g.normal_output();
        assert!(near(luma(normal, 10, 20), 50), "normal left {}", luma(normal, 10, 20));
        assert!(near(luma(normal, 50, 20), 200), "normal right {}", luma(normal, 50, 20));
        assert!(near(luma(normal, 3, 3), RED_LUMA), "normal overlay {}", luma(normal, 3, 3));
        // And the mirrored corner carries no overlay on either feed.
        assert!(!near(luma(send, W - 4, 3), RED_LUMA));
        assert!(!near(luma(normal, W - 4, 3), RED_LUMA));

        // The SEND flip survives the detour: the next frame is mirrored again.
        g.process(&split_frame()).unwrap();
        assert!(near(luma(g.output(), 10, 20), 200));
        assert!(g.flipped());
    }

    #[test]
    fn normal_pass_keeps_a_replacement_background_in_place() {
        let Some(mut g) = gpu(1) else { return };
        // Background: opaque white on the left, black on the right, and a
        // mask that says "nobody here" so the background shows everywhere.
        let mut bg = solid(W, H, 0xff00_0000);
        for y in 0..H {
            for x in 0..W / 2 {
                bg.pixels[(y * W + x) as usize] = 0xffff_ffff;
            }
        }
        g.set_background(Some(&bg));
        g.set_mask(&vec![0u8; (W * H) as usize], W, H);
        g.process(&split_frame()).unwrap();
        g.process_normal().unwrap();

        // A replacement is placed in output pixels, so it must not mirror on
        // either feed: white stays on the left of both.
        for (name, frame) in [("send", g.output()), ("normal", g.normal_output())] {
            assert!(near(luma(frame, 10, 20), 235), "{name} left {}", luma(frame, 10, 20));
            assert!(near(luma(frame, 50, 20), 16), "{name} right {}", luma(frame, 50, 20));
        }
    }

    #[test]
    fn unflipped_send_needs_no_normal_pass() {
        let Some(mut g) = gpu(0) else { return };
        g.process(&split_frame()).unwrap();
        assert!(!g.flipped());
        assert!(near(luma(g.output(), 10, 20), 50));
    }
}
