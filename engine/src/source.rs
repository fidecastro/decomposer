//! Frame sources and the virtual-camera sink.
//!
//! Call mode reads the camera's own V4L2 node directly — the kernel already
//! owns that path, so there is no reason to route pixels through Python.
//! Studio mode has no V4L2 node at all (the camera reboots into DepthAI
//! firmware), so frames arrive as raw NV12 on stdin from the depthai layer.

use anyhow::{bail, Context, Result};
use nix::errno::Errno;
use std::io::{self, Read};
use std::mem::{self, ManuallyDrop};
use std::os::fd::FromRawFd;
use v4l::buffer::Type;
use v4l::io::traits::{CaptureStream, OutputStream};
use v4l::prelude::*;
use v4l::video::{Capture, Output};
use v4l::v4l_sys::{v4l2_event, v4l2_event_subscription};
use v4l::{Format, FourCC};

/// linux/videodev2.h: this frame is progressive, not one field of an
/// interlaced pair.
const V4L2_FIELD_NONE: u32 = 1;

// v4l2loopback 0.15's private event. The driver sends one whenever a capture
// client STREAMONs or STREAMOFFs, including an initial state on subscribe.
const V4L2_EVENT_PRIVATE_START: u32 = 0x0800_0000;
const V4L2LOOPBACK_EVENT_OFFSET: u32 = 0x08e0_0000;
const V4L2_EVENT_PRI_CLIENT_USAGE: u32 =
    V4L2_EVENT_PRIVATE_START + V4L2LOOPBACK_EVENT_OFFSET + 1;
const V4L2_EVENT_SUB_FL_SEND_INITIAL: u32 = 1;

nix::ioctl_read!(vidioc_dqevent, b'V', 89, v4l2_event);
nix::ioctl_write_ptr!(vidioc_subscribe_event, b'V', 90, v4l2_event_subscription);

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
    path: String,
    primed: bool,
    viewer_active: bool,
    usage_events: bool,
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
                 Is v4l2loopback loaded? See docs/SETUP.md",
                got.width, got.height, got.fourcc
            );
        }

        let usage_events = subscribe_client_usage(dev).is_ok();
        if !usage_events {
            eprintln!(
                "output {path}: viewer detection unavailable; publishing continuously"
            );
        }
        let stream = MmapStream::with_buffers(dev, Type::VideoOutput, 2)
            .context("start output stream")?;
        Ok(Self {
            stream,
            width,
            height,
            path: path.to_string(),
            primed: false,
            viewer_active: !usage_events,
            usage_events,
        })
    }

    /// Publish only while a capture client is streaming. The first frame is
    /// always prepared because STREAMON on the producer is what makes an
    /// exclusive-caps v4l2loopback node visible to camera pickers.
    pub fn write_if_watched(&mut self, frame: &[u8], flip: u32) -> Result<bool> {
        self.refresh_viewer();
        if self.primed && self.usage_events && !self.viewer_active {
            return Ok(false);
        }

        let y_len = (self.width * self.height) as usize;
        let total = y_len + y_len / 2;
        if frame.len() < total {
            bail!("short frame: {} bytes, need {total}", frame.len());
        }
        let (buf, meta) = OutputStream::next(&mut self.stream)?;
        if buf.len() < total {
            bail!("output buffer too small: {} < {total}", buf.len());
        }
        nv12_to_i420(frame, &mut buf[..total], self.width, self.height, flip)?;
        // V4L2_FIELD_NONE. Not ANY (0), which tells the driver it may choose,
        // and leaves a consumer free to treat the buffer as a single field
        // rather than a whole progressive frame.
        meta.field = V4L2_FIELD_NONE;
        meta.bytesused = total as u32;
        self.primed = true;
        Ok(true)
    }

    fn refresh_viewer(&mut self) {
        if !self.usage_events {
            return;
        }
        loop {
            let mut event: v4l2_event = unsafe { mem::zeroed() };
            match unsafe { vidioc_dqevent(self.stream.handle().fd(), &mut event) } {
                Ok(_) => {
                    if event.type_ != V4L2_EVENT_PRI_CLIENT_USAGE {
                        continue;
                    }
                    let data = unsafe { event.u.data };
                    let active = u32::from_ne_bytes([data[0], data[1], data[2], data[3]]) != 0;
                    if active != self.viewer_active {
                        self.viewer_active = active;
                        eprintln!(
                            "output {}: {}",
                            self.path,
                            if active { "viewer connected" } else { "idle" },
                        );
                    }
                }
                // The V4L2 core normally reports EAGAIN for an empty
                // nonblocking event queue. v4l2loopback 0.15 also reports
                // ENOENT after its initial event has been consumed.
                Err(Errno::EAGAIN | Errno::ENOENT) => return,
                Err(e) => {
                    self.usage_events = false;
                    self.viewer_active = true;
                    eprintln!(
                        "output {}: viewer event failed ({e}); publishing continuously",
                        self.path,
                    );
                    return;
                }
            }
        }
    }
}

fn subscribe_client_usage(dev: &Device) -> io::Result<()> {
    let subscription = v4l2_event_subscription {
        type_: V4L2_EVENT_PRI_CLIENT_USAGE,
        flags: V4L2_EVENT_SUB_FL_SEND_INITIAL,
        ..unsafe { mem::zeroed() }
    };
    unsafe { vidioc_subscribe_event(dev.handle().fd(), &subscription) }
        .map(|_| ())
        .map_err(|e| io::Error::from_raw_os_error(e as i32))
}

/// Convert NV12 to I420 while optionally applying a final output flip.
/// Applying the same flip after the GPU is an involution, which gives the
/// second sink a stable normal orientation without another shader pass.
fn nv12_to_i420(
    src: &[u8],
    dst: &mut [u8],
    width: u32,
    height: u32,
    flip: u32,
) -> Result<()> {
    let (w, h) = (width as usize, height as usize);
    let y_len = w * h;
    let chroma_w = w / 2;
    let chroma_h = h / 2;
    let quarter = chroma_w * chroma_h;
    let total = y_len + 2 * quarter;
    if src.len() < total || dst.len() < total {
        bail!(
            "short conversion buffer: source {}, destination {}, need {total}",
            src.len(),
            dst.len(),
        );
    }
    let flip_h = flip & 1 != 0;
    let flip_v = flip & 2 != 0;

    if !flip_h && !flip_v {
        dst[..y_len].copy_from_slice(&src[..y_len]);
    } else {
        for out_y in 0..h {
            let src_y = if flip_v { h - 1 - out_y } else { out_y };
            for out_x in 0..w {
                let src_x = if flip_h { w - 1 - out_x } else { out_x };
                dst[out_y * w + out_x] = src[src_y * w + src_x];
            }
        }
    }

    let (u_out, v_out) = dst[y_len..total].split_at_mut(quarter);
    for out_y in 0..chroma_h {
        let src_y = if flip_v { chroma_h - 1 - out_y } else { out_y };
        for out_x in 0..chroma_w {
            let src_x = if flip_h { chroma_w - 1 - out_x } else { out_x };
            let src_i = y_len + (src_y * chroma_w + src_x) * 2;
            let out_i = out_y * chroma_w + out_x;
            u_out[out_i] = src[src_i];
            v_out[out_i] = src[src_i + 1];
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::nv12_to_i420;

    fn converted(src: &[u8], width: u32, height: u32, flip: u32) -> Vec<u8> {
        let mut dst = vec![0; src.len()];
        nv12_to_i420(src, &mut dst, width, height, flip).unwrap();
        dst
    }

    #[test]
    fn converts_nv12_to_i420_without_a_flip() {
        let src = [0, 1, 2, 3, 4, 5, 6, 7, 10, 20, 30, 40];
        assert_eq!(
            converted(&src, 4, 2, 0),
            [0, 1, 2, 3, 4, 5, 6, 7, 10, 30, 20, 40],
        );
    }

    #[test]
    fn horizontal_flip_reverses_pixels_and_chroma_pairs() {
        let src = [0, 1, 2, 3, 4, 5, 6, 7, 10, 20, 30, 40];
        assert_eq!(
            converted(&src, 4, 2, 1),
            [3, 2, 1, 0, 7, 6, 5, 4, 30, 10, 40, 20],
        );
    }

    #[test]
    fn vertical_and_180_flips_reverse_the_expected_rows() {
        let src = [
            0, 1, 2, 3,
            4, 5, 6, 7,
            8, 9, 10, 11,
            12, 13, 14, 15,
            20, 30, 40, 50,
            60, 70, 80, 90,
        ];
        assert_eq!(
            converted(&src, 4, 4, 2),
            [
                12, 13, 14, 15,
                8, 9, 10, 11,
                4, 5, 6, 7,
                0, 1, 2, 3,
                60, 80, 20, 40,
                70, 90, 30, 50,
            ],
        );
        assert_eq!(
            converted(&src, 4, 4, 3),
            [
                15, 14, 13, 12,
                11, 10, 9, 8,
                7, 6, 5, 4,
                3, 2, 1, 0,
                80, 60, 40, 20,
                90, 70, 50, 30,
            ],
        );
    }
}
