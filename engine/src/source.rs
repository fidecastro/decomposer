//! Frame sources and the virtual-camera sink.
//!
//! Call mode reads the camera's own V4L2 node directly — the kernel already
//! owns that path, so there is no reason to route pixels through Python.
//! Studio mode has no V4L2 node at all (the camera reboots into DepthAI
//! firmware), so frames arrive as raw NV12 on stdin from the depthai layer.

use anyhow::{bail, Context, Result};
use nix::errno::Errno;
use std::collections::VecDeque;
use std::io::{self, Read};
use std::mem::{self, ManuallyDrop};
use std::os::fd::FromRawFd;
use std::os::raw::{c_int, c_void};
use std::ptr;
use std::slice;
use std::sync::Arc;
use std::time::{Duration, Instant};
use v4l::buffer::Type;
use v4l::device::Handle;
use v4l::io::traits::CaptureStream;
use v4l::memory::Memory;
use v4l::prelude::*;
use v4l::v4l2;
use v4l::video::{Capture, Output};
use v4l::v4l_sys::{v4l2_buffer, v4l2_event, v4l2_event_subscription, v4l2_requestbuffers};
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

/// While nobody is streaming from a node, a keep-warm frame every 100 ms
/// keeps its ring current. A capture client that connects is handed the most
/// recently queued frame, timestamp and all, before anything new arrives:
/// without this the first frame a viewer saw was whatever the engine started
/// on - typically the camera's black warm-up frame. The interval bounds how
/// old that first frame can be. Measured at one second, ffmpeg bridged the
/// timestamp gap with thirty duplicate frames and a recording opened on a
/// one-second freeze; at 100 ms neither is visible, for a third of the copy
/// work of a live viewer.
const IDLE_REFRESH: Duration = Duration::from_millis(100);
const OUTPUT_BUFFERS: u32 = 2;

pub struct V4l2Sink {
    handle: Arc<Handle>,
    /// The driver's mmapped output buffers, in index order.
    buffers: Vec<&'static mut [u8]>,
    /// Buffers never handed to the driver yet; once these run out, every
    /// write asks the driver for a finished buffer with DQBUF.
    free: VecDeque<usize>,
    streaming: bool,
    width: u32,
    height: u32,
    path: String,
    primed: bool,
    viewer_active: bool,
    usage_events: bool,
    last_write: Option<Instant>,
}

impl V4l2Sink {
    pub fn new(path: &str, width: u32, height: u32) -> Result<Self> {
        let dev = Device::with_path(path).with_context(|| format!("open {path}"))?;

        // The virtual camera speaks I420 (YU12), not NV12, deliberately.
        // OBS consumes this device through libv4l2's emulated formats, and
        // libv4lconvert's NV12 conversion path flips the frame vertically -
        // measured, not surmised: a frame with a red band at rows 150-210 and
        // a blue band at 415-450 comes out with red at 855-929 and blue at
        // 630-661, the exact mirror positions. Publishing I420 lets every
        // consumer take the frames natively and the flipping code never runs.
        let want = Format::new(width, height, FourCC::new(b"YU12"));
        let got = Output::set_format(&dev, &want).context("set output format")?;
        if got.width != width || got.height != height || &got.fourcc.repr != b"YU12" {
            bail!(
                "{path} rejected {width}x{height} YU12 (got {}x{} {}). \
                 Is v4l2loopback loaded? See docs/SETUP.md",
                got.width, got.height, got.fourcc
            );
        }

        let usage_events = subscribe_client_usage(&dev).is_ok();
        if !usage_events {
            eprintln!(
                "output {path}: viewer detection unavailable; publishing continuously"
            );
        }
        // The buffers are managed here rather than through the crate's
        // output stream: that stream only queues a buffer on the *next* call,
        // so every frame reached viewers one write late, and a frame written
        // while idle never reached them at all.
        let handle = dev.handle();
        let buffers = map_output_buffers(&handle, OUTPUT_BUFFERS)?;
        let free = (0..buffers.len()).collect();
        Ok(Self {
            handle,
            buffers,
            free,
            streaming: false,
            width,
            height,
            path: path.to_string(),
            primed: false,
            viewer_active: !usage_events,
            usage_events,
            last_write: None,
        })
    }

    /// Whether the next frame should be converted and published here.
    ///
    /// The first frame always is: STREAMON on the producer is what makes an
    /// exclusive-caps v4l2loopback node visible to camera pickers. After
    /// that, frames flow while a capture client is streaming, plus a
    /// keep-warm frame every 100 ms while nobody is.
    pub fn wants_frame(&mut self) -> bool {
        self.refresh_viewer();
        should_publish(
            self.primed,
            self.usage_events,
            self.viewer_active,
            self.last_write.map(|t| t.elapsed()),
        )
    }

    /// Publish one NV12 frame now, converted to I420.
    pub fn write(&mut self, frame: &[u8]) -> Result<()> {
        let y_len = (self.width * self.height) as usize;
        let total = y_len + y_len / 2;
        if frame.len() < total {
            bail!("short frame: {} bytes, need {total}", frame.len());
        }
        let index = match self.free.pop_front() {
            Some(i) => i,
            None => self.dequeue()?,
        };
        {
            let buf = &mut self.buffers[index];
            if buf.len() < total {
                bail!("output buffer too small: {} < {total}", buf.len());
            }
            nv12_to_i420(frame, &mut buf[..total], self.width, self.height)?;
        }
        if !self.streaming {
            self.stream_on()?;
            self.streaming = true;
        }
        // Queued immediately: the frame is visible to readers before this
        // function returns, not one write later.
        self.queue(index, total)?;
        self.primed = true;
        self.last_write = Some(Instant::now());
        Ok(())
    }

    /// `wants_frame` and `write` in one step, for the sink whose frame needs
    /// no extra work to prepare.
    pub fn write_if_watched(&mut self, frame: &[u8]) -> Result<bool> {
        if !self.wants_frame() {
            return Ok(false);
        }
        self.write(frame)?;
        Ok(true)
    }

    fn buffer_desc(&self, index: usize) -> v4l2_buffer {
        v4l2_buffer {
            index: index as u32,
            type_: Type::VideoOutput as u32,
            memory: Memory::Mmap as u32,
            ..unsafe { mem::zeroed() }
        }
    }

    fn queue(&mut self, index: usize, bytesused: usize) -> Result<()> {
        let mut desc = self.buffer_desc(index);
        desc.bytesused = bytesused as u32;
        // V4L2_FIELD_NONE. Not ANY (0), which tells the driver it may choose,
        // and leaves a consumer free to treat the buffer as a single field
        // rather than a whole progressive frame.
        desc.field = V4L2_FIELD_NONE;
        unsafe {
            v4l2::ioctl(
                self.handle.fd(),
                v4l2::vidioc::VIDIOC_QBUF,
                &mut desc as *mut _ as *mut c_void,
            )
        }
        .with_context(|| format!("queue output buffer on {}", self.path))
    }

    fn dequeue(&mut self) -> Result<usize> {
        let mut desc = self.buffer_desc(0);
        unsafe {
            v4l2::ioctl(
                self.handle.fd(),
                v4l2::vidioc::VIDIOC_DQBUF,
                &mut desc as *mut _ as *mut c_void,
            )
        }
        .with_context(|| format!("dequeue output buffer on {}", self.path))?;
        let index = desc.index as usize;
        if index >= self.buffers.len() {
            bail!("driver returned output buffer {index} of {}", self.buffers.len());
        }
        Ok(index)
    }

    fn stream_on(&mut self) -> Result<()> {
        let mut kind = Type::VideoOutput as c_int;
        unsafe {
            v4l2::ioctl(
                self.handle.fd(),
                v4l2::vidioc::VIDIOC_STREAMON,
                &mut kind as *mut _ as *mut c_void,
            )
        }
        .with_context(|| format!("start output stream on {}", self.path))
    }

    fn refresh_viewer(&mut self) {
        if !self.usage_events {
            return;
        }
        loop {
            let mut event: v4l2_event = unsafe { mem::zeroed() };
            match unsafe { vidioc_dqevent(self.handle.fd(), &mut event) } {
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
                // The V4L2 core answers ENOENT for an empty non-blocking event
                // queue (v4l2_event_dequeue). EAGAIN is matched as well in
                // case a driver follows the read(2) convention instead.
                Err(Errno::ENOENT | Errno::EAGAIN) => return,
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

impl Drop for V4l2Sink {
    fn drop(&mut self) {
        for buf in self.buffers.drain(..) {
            let _ = unsafe { v4l2::munmap(buf.as_mut_ptr() as *mut c_void, buf.len()) };
        }
    }
}

/// The publishing policy, kept free of device state so it can be tested:
/// prime once, then follow the viewer, with a keep-warm cadence while idle.
fn should_publish(
    primed: bool,
    usage_events: bool,
    viewer_active: bool,
    since_last_write: Option<Duration>,
) -> bool {
    if !primed || !usage_events || viewer_active {
        return true;
    }
    since_last_write.map_or(true, |idle| idle >= IDLE_REFRESH)
}

/// REQBUFS + QUERYBUF + mmap for an output queue. The mappings live for the
/// process: the sink is created once and dropped at exit.
fn map_output_buffers(handle: &Handle, count: u32) -> Result<Vec<&'static mut [u8]>> {
    let mut request = v4l2_requestbuffers {
        count,
        type_: Type::VideoOutput as u32,
        memory: Memory::Mmap as u32,
        ..unsafe { mem::zeroed() }
    };
    unsafe {
        v4l2::ioctl(
            handle.fd(),
            v4l2::vidioc::VIDIOC_REQBUFS,
            &mut request as *mut _ as *mut c_void,
        )
    }
    .context("request output buffers")?;
    if request.count == 0 {
        bail!("driver granted no output buffers");
    }
    let mut buffers = Vec::with_capacity(request.count as usize);
    for index in 0..request.count {
        let mut desc = v4l2_buffer {
            index,
            type_: Type::VideoOutput as u32,
            memory: Memory::Mmap as u32,
            ..unsafe { mem::zeroed() }
        };
        unsafe {
            v4l2::ioctl(
                handle.fd(),
                v4l2::vidioc::VIDIOC_QUERYBUF,
                &mut desc as *mut _ as *mut c_void,
            )
        }
        .with_context(|| format!("query output buffer {index}"))?;
        let len = desc.length as usize;
        let offset = unsafe { desc.m.offset } as libc::off_t;
        let ptr = unsafe {
            v4l2::mmap(
                ptr::null_mut(),
                len,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_SHARED,
                handle.fd(),
                offset,
            )
        }
        .with_context(|| format!("map output buffer {index}"))?;
        buffers.push(unsafe { slice::from_raw_parts_mut(ptr as *mut u8, len) });
    }
    Ok(buffers)
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

/// Convert NV12 (interleaved chroma) to I420 (planar chroma). Both feeds
/// are rendered in their own orientation on the GPU, so no geometry changes
/// here: this is a copy and a deinterleave.
fn nv12_to_i420(src: &[u8], dst: &mut [u8], width: u32, height: u32) -> Result<()> {
    let (w, h) = (width as usize, height as usize);
    let y_len = w * h;
    let quarter = (w / 2) * (h / 2);
    let total = y_len + 2 * quarter;
    if src.len() < total || dst.len() < total {
        bail!(
            "short conversion buffer: source {}, destination {}, need {total}",
            src.len(),
            dst.len(),
        );
    }
    dst[..y_len].copy_from_slice(&src[..y_len]);
    let (u_out, v_out) = dst[y_len..total].split_at_mut(quarter);
    for (i, pair) in src[y_len..total].chunks_exact(2).enumerate() {
        u_out[i] = pair[0];
        v_out[i] = pair[1];
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{nv12_to_i420, should_publish, IDLE_REFRESH};
    use std::time::Duration;

    fn converted(src: &[u8], width: u32, height: u32) -> Vec<u8> {
        let mut dst = vec![0; src.len()];
        nv12_to_i420(src, &mut dst, width, height).unwrap();
        dst
    }

    #[test]
    fn converts_nv12_to_i420() {
        let src = [0, 1, 2, 3, 4, 5, 6, 7, 10, 20, 30, 40];
        assert_eq!(converted(&src, 4, 2), [0, 1, 2, 3, 4, 5, 6, 7, 10, 30, 20, 40]);
    }

    #[test]
    fn deinterleaves_chroma_rows_in_order() {
        let src = [
            0, 1, 2, 3,
            4, 5, 6, 7,
            8, 9, 10, 11,
            12, 13, 14, 15,
            20, 30, 40, 50,
            60, 70, 80, 90,
        ];
        assert_eq!(
            converted(&src, 4, 4),
            [
                0, 1, 2, 3,
                4, 5, 6, 7,
                8, 9, 10, 11,
                12, 13, 14, 15,
                20, 40, 60, 80,
                30, 50, 70, 90,
            ],
        );
    }

    #[test]
    fn short_buffers_are_refused() {
        let src = [0u8; 12];
        let mut dst = [0u8; 11];
        assert!(nv12_to_i420(&src, &mut dst, 4, 2).is_err());
    }

    #[test]
    fn priming_frame_is_always_published() {
        assert!(should_publish(false, true, false, None));
    }

    #[test]
    fn viewer_or_missing_events_publish_every_frame() {
        assert!(should_publish(true, true, true, Some(Duration::ZERO)));
        assert!(should_publish(true, false, false, Some(Duration::ZERO)));
    }

    #[test]
    fn idle_node_gets_a_keep_warm_frame_every_hundred_ms() {
        assert!(!should_publish(true, true, false, Some(Duration::from_millis(33))));
        assert!(!should_publish(true, true, false, Some(IDLE_REFRESH - Duration::from_millis(1))));
        assert!(should_publish(true, true, false, Some(IDLE_REFRESH)));
        assert!(should_publish(true, true, false, None));
    }
}
