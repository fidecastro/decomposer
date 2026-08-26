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
}

pub struct Gpu {
    device: wgpu::Device,
    queue: wgpu::Queue,
    pipeline: wgpu::ComputePipeline,
    bind_group: wgpu::BindGroup,
    params_buf: wgpu::Buffer,
    src_buf: wgpu::Buffer,
    dst_buf: wgpu::Buffer,
    staging: wgpu::Buffer,
    params: Params,
    size: u64,
    out: Vec<u8>,
    pub adapter_name: String,
}

impl Gpu {
    pub fn new(width: u32, height: u32, look: u32, strength: f32) -> Result<Self> {
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
        let params = Params { width, height, look, strength };

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
            ],
        });

        let bind_group = device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("look-bind"),
            layout: &layout,
            entries: &[
                wgpu::BindGroupEntry { binding: 0, resource: params_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 1, resource: src_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 2, resource: dst_buf.as_entire_binding() },
            ],
        });

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
            device, queue, pipeline, bind_group, params_buf, src_buf, dst_buf,
            staging, params, size, out: vec![0u8; size as usize], adapter_name,
        })
    }

    /// Change the look without rebuilding the pipeline. Used by the daemon
    /// when the user switches look at runtime.
    #[allow(dead_code)]
    pub fn set_look(&mut self, look: u32, strength: f32) {
        self.params.look = look;
        self.params.strength = strength;
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
