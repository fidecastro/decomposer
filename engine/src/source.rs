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

/// linux/videodev2.h: this frame is progressive, not one field of an
/// interlaced pair.
const V4L2_FIELD_NONE: u32 = 1;

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
    width: u32,
    height: u32,
}

impl V4l2Sink {
    pub fn new(path: &str, width: u32, height: u32) -> Result<Self> {
        let dev: &'static Device = Box::leak(Box::new(
            Device::with_path(path).with_context(|| format!("open {path}"))?,
        ));

        // The virtual camera speaks I420 (YU12), not NV12, deliberately.
        // OBS consumes this device through libv4l2's emulated formats, and
        // libv4lconvert's NV12 conversion path flips the frame vertically -
        // measured, not surmised: a frame with a red band at rows 150-210 and
        // a blue band at 415-450 comes out with red at 855-929 and blue at
        // 630-661, the exact mirror positions. Publishing I420 lets every
        // consumer take the frames natively and the flipping code never runs.
        let want = Format::new(width, height, FourCC::new(b"YU12"));
        let got = Output::set_format(dev, &want).context("set output format")?;
        if got.width != width || got.height != height || &got.fourcc.repr != b"YU12" {
            bail!(
                "{path} rejected {width}x{height} YU12 (got {}x{} {}). \
                 Is v4l2loopback loaded? See packaging/v4l2loopback.conf",
                got.width, got.height, got.fourcc
            );
        }

        let stream = MmapStream::with_buffers(dev, Type::VideoOutput, 2)
            .context("start output stream")?;
        Ok(Self { stream, width, height })
    }

    /// Publish one NV12 frame as I420: Y verbatim, then the interleaved UV
    /// plane split into planar U and V.
    pub fn write(&mut self, frame: &[u8]) -> Result<()> {
        let y_len = (self.width * self.height) as usize;
        let quarter = y_len / 4;
        let total = y_len + 2 * quarter;
        if frame.len() < total {
            bail!("short frame: {} bytes, need {total}", frame.len());
        }
        let (buf, meta) = OutputStream::next(&mut self.stream)?;
        if buf.len() < total {
            bail!("output buffer too small: {} < {total}", buf.len());
        }
        buf[..y_len].copy_from_slice(&frame[..y_len]);
        let (u_out, v_out) = buf[y_len..y_len + 2 * quarter].split_at_mut(quarter);
        for (i, pair) in frame[y_len..total].chunks_exact(2).enumerate() {
            u_out[i] = pair[0];
            v_out[i] = pair[1];
        }
        // V4L2_FIELD_NONE. Not ANY (0), which tells the driver it may choose,
        // and leaves a consumer free to treat the buffer as a single field
        // rather than a whole progressive frame.
        meta.field = V4L2_FIELD_NONE;
        meta.bytesused = total as u32;
        Ok(())
    }
}
