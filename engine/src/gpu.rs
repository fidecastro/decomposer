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
    // WGSL rounds uniform structs up to 16 bytes; pad explicitly so the Rust
    // and shader layouts cannot silently disagree.
    _pad: [u32; 2],
}

pub struct Gpu {
    device: wgpu::Device,
    queue: wgpu::Queue,
    pipeline: wgpu::ComputePipeline,
    bind_group: wgpu::BindGroup,
    layout: wgpu::BindGroupLayout,
    params_buf: wgpu::Buffer,
    src_buf: wgpu::Buffer,
    dst_buf: wgpu::Buffer,
    overlay_buf: wgpu::Buffer,
    staging: wgpu::Buffer,
    params: Params,
    size: u64,
    out: Vec<u8>,
    pub adapter_name: String,
}

impl Gpu {
    pub fn new(width: u32, height: u32, look: u32, strength: f32, flip: u32) -> Result<Self> {
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
                ..Default::default()
            }))
            .map_err(|e| anyhow!("could not create GPU device: {e}"))?;

        let size = super::source::nv12_len(width, height) as u64;
        let params = Params {
            width, height, look, strength, flip,
            ov_x: 0, ov_y: 0, ov_w: 0, ov_h: 0, ov_opacity: 1.0,
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
        let src_buf = mk("src", wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST);
        let dst_buf = mk("dst", wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC);
        let staging = mk("staging", wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST);
        // A storage binding cannot be empty, so an absent overlay is a single
        // transparent pixel that the shader never reads (ov_w stays 0).
        let overlay_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("overlay"),
            contents: bytemuck::cast_slice(&[0u32]),
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
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
            ],
        });

        let bind_group = make_bind_group(
            &device, &layout, &params_buf, &src_buf, &dst_buf, &overlay_buf,
        );

        let pipeline_layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
            label: Some("look-pipeline-layout"),
            bind_group_layouts: &[Some(&layout)],
            immediate_size: 0,
        });
        let pipeline = device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some("look-pipeline"),
            layout: Some(&pipeline_layout),
            module: &shader,
            entry_point: Some("main"),
            compilation_options: Default::default(),
            cache: None,
        });

        Ok(Self {
            device, queue, pipeline, bind_group, layout, params_buf, src_buf, dst_buf,
            overlay_buf, staging, params, size, out: vec![0u8; size as usize], adapter_name,
        })
    }

    /// Change the look without rebuilding the pipeline. Used by the daemon
    /// when the user switches look at runtime.
    pub fn set_look(&mut self, look: u32, strength: f32) {
        self.params.look = look;
        self.params.strength = strength;
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

    fn upload_params(&mut self) {
        self.queue
            .write_buffer(&self.params_buf, 0, bytemuck::bytes_of(&self.params));
    }

    /// Grade one NV12 frame. The returned slice is valid until the next call.
    pub fn process(&mut self, frame: &[u8]) -> Result<&[u8]> {
        let n = (frame.len() as u64).min(self.size);
        self.queue.write_buffer(&self.src_buf, 0, &frame[..n as usize]);

        let mut enc = self
            .device
            .create_command_encoder(&wgpu::CommandEncoderDescriptor { label: Some("look") });
        {
            let mut pass = enc.begin_compute_pass(&wgpu::ComputePassDescriptor {
                label: Some("look-pass"),
                timestamp_writes: None,
            });
            pass.set_pipeline(&self.pipeline);
            pass.set_bind_group(0, &self.bind_group, &[]);
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
            self.out.copy_from_slice(&view[..]);
        }
        self.staging.unmap();
        Ok(&self.out)
    }
}

fn make_bind_group(
    device: &wgpu::Device,
    layout: &wgpu::BindGroupLayout,
    params: &wgpu::Buffer,
    src: &wgpu::Buffer,
    dst: &wgpu::Buffer,
    overlay: &wgpu::Buffer,
) -> wgpu::BindGroup {
    device.create_bind_group(&wgpu::BindGroupDescriptor {
        label: Some("look-bind"),
        layout,
        entries: &[
            wgpu::BindGroupEntry { binding: 0, resource: params.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 1, resource: src.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 2, resource: dst.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 3, resource: overlay.as_entire_binding() },
        ],
    })
}
