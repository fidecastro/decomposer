//! Frame sources and the virtual-camera sink.
//!
//! Call mode reads the camera's own V4L2 node directly — the kernel already
//! owns that path, so there is no reason to route pixels through Python.
//! Studio mode has no V4L2 node at all (the camera reboots into DepthAI
//! firmware), so frames arrive as raw NV12 on stdin from the depthai layer.

use anyhow::{bail, Context, Result};
use std::io::Read;
use std::mem::ManuallyDrop;
use std::os::fd::FromRawFd;
use v4l::buffer::Type;
use v4l::io::traits::{CaptureStream, OutputStream};
use v4l::prelude::*;
use v4l::video::{Capture, Output};
use v4l::{Format, FourCC};

/// NV12 is 8 bits of luma per pixel plus a half-resolution interleaved
/// chroma plane, so one frame is width * height * 3 / 2 bytes.
pub fn nv12_len(width: u32, height: u32) -> usize {
    (width as usize * height as usize * 3) / 2
}

pub trait FrameSource {
    fn dimensions(&self) -> (u32, u32);
    /// Next NV12 frame, or None when the source is exhausted.
    fn next_frame(&mut self) -> Result<Option<&[u8]>>;
}

pub struct V4l2Source {
    stream: MmapStream<'static>,
    width: u32,
    height: u32,
}

impl V4l2Source {
    pub fn new(path: &str, width: u32, height: u32) -> Result<Self> {
        // The stream borrows the device for its whole life. This process holds
        // exactly one capture device until it exits, so leaking it is simpler
        // and safer than a self-referential struct.
        let dev: &'static Device = Box::leak(Box::new(
            Device::with_path(path).with_context(|| format!("open {path}"))?,
        ));

        let mut fmt = Capture::format(dev).context("query capture format")?;
        fmt.width = width;
        fmt.height = height;
        fmt.fourcc = FourCC::new(b"NV12");
        let fmt = Capture::set_format(dev, &fmt).context("set capture format")?;

        if &fmt.fourcc.repr != b"NV12" {
            bail!(
                "{path} would not accept NV12 (got {}). The Opal C1 offers NV12 only.",
                fmt.fourcc
            );
        }

        let stream = MmapStream::with_buffers(dev, Type::VideoCapture, 4)
            .context("start capture stream")?;
        Ok(Self { stream, width: fmt.width, height: fmt.height })
    }
}

impl FrameSource for V4l2Source {
    fn dimensions(&self) -> (u32, u32) {
        (self.width, self.height)
    }

    fn next_frame(&mut self) -> Result<Option<&[u8]>> {
        let (buf, meta) = CaptureStream::next(&mut self.stream)?;
        let used = meta.bytesused as usize;
        Ok(Some(&buf[..used.min(buf.len())]))
    }
}

pub struct StdinSource {
    // fd 0 directly rather than std::io::stdin(): that wraps a BufReader whose
    // extra copy costs about 10% of frame rate at 3 MB per frame. ManuallyDrop
    // keeps it from closing stdin when the source is dropped.
    stdin: ManuallyDrop<std::fs::File>,
    buf: Vec<u8>,
    width: u32,
    height: u32,
    eof: bool,
}

impl StdinSource {
    pub fn new(width: u32, height: u32) -> Self {
        Self {
            stdin: ManuallyDrop::new(unsafe { std::fs::File::from_raw_fd(0) }),
            buf: vec![0u8; nv12_len(width, height)],
            width,
            height,
            eof: false,
        }
    }
}

impl FrameSource for StdinSource {
    fn dimensions(&self) -> (u32, u32) {
        (self.width, self.height)
    }

    fn next_frame(&mut self) -> Result<Option<&[u8]>> {
        if self.eof {
            return Ok(None);
        }
        match self.stdin.read_exact(&mut self.buf) {
            Ok(()) => Ok(Some(&self.buf)),
            Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => {
                self.eof = true;
                Ok(None)
            }
            Err(e) => Err(e).context("reading NV12 from stdin"),
        }
    }
}

pub struct V4l2Sink {
    stream: MmapStream<'static>,
}

impl V4l2Sink {
    pub fn new(path: &str, width: u32, height: u32) -> Result<Self> {
        let dev: &'static Device = Box::leak(Box::new(
            Device::with_path(path).with_context(|| format!("open {path}"))?,
        ));

        let want = Format::new(width, height, FourCC::new(b"NV12"));
        let got = Output::set_format(dev, &want).context("set output format")?;
        if got.width != width || got.height != height || &got.fourcc.repr != b"NV12" {
            bail!(
                "{path} rejected {width}x{height} NV12 (got {}x{} {}). \
                 Is v4l2loopback loaded? See packaging/v4l2loopback.conf",
                got.width, got.height, got.fourcc
            );
        }

        let stream = MmapStream::with_buffers(dev, Type::VideoOutput, 2)
            .context("start output stream")?;
        Ok(Self { stream })
    }

    pub fn write(&mut self, frame: &[u8]) -> Result<()> {
        let (buf, meta) = OutputStream::next(&mut self.stream)?;
        let n = frame.len().min(buf.len());
        buf[..n].copy_from_slice(&frame[..n]);
        meta.field = 0;
        meta.bytesused = n as u32;
        Ok(())
    }
}
