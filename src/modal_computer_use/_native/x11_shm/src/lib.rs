//! Persistent X11 shared-memory capture for complete lossless screenshots.
//!
//! This crate is intentionally a small native seam.  Modal orchestration,
//! screenshot metadata, authentication, fallback policy, and SDK validation
//! remain in Python.  The native side owns one reusable MIT-SHM buffer and
//! turns an X11 root-window image into a complete PNG with the same decoded
//! pixels as the existing screenshot route.

#![cfg_attr(not(target_os = "linux"), allow(dead_code))]

use anyhow::{anyhow, bail, Context, Result};
#[cfg(target_os = "linux")]
use std::slice;

#[cfg(target_os = "linux")]
use std::ffi::{c_void, CString};
#[cfg(target_os = "linux")]
use std::fmt;
#[cfg(target_os = "linux")]
use std::io;
#[cfg(target_os = "linux")]
use std::os::fd::{AsRawFd, RawFd};
#[cfg(target_os = "linux")]
use std::ptr::NonNull;
#[cfg(target_os = "linux")]
use std::time::{Duration, Instant};

#[cfg(all(target_os = "linux", feature = "extension-module"))]
use pyo3::exceptions::{PyRuntimeError, PyValueError};
#[cfg(all(target_os = "linux", feature = "extension-module"))]
use pyo3::prelude::*;
#[cfg(all(target_os = "linux", feature = "extension-module"))]
use pyo3::types::{PyBytes, PyModule};

#[cfg(all(target_os = "linux", feature = "extension-module"))]
pyo3::create_exception!(
    _modal_computer_use_x11_shm,
    X11ScreenshotTimeoutError,
    PyRuntimeError
);

#[cfg(target_os = "linux")]
use xcb::{shm, x, Connection};

const BACKEND_MARKER: &str = "x11-shm";
const CODEC_MARKER: &str = "png-deflate-level2-no-filter";
const X11_REPLY_TIMEOUT_MS: u64 = 750;
#[cfg(target_os = "linux")]
const X11_REPLY_TIMEOUT: Duration = Duration::from_millis(X11_REPLY_TIMEOUT_MS);

#[cfg(target_os = "linux")]
#[derive(Debug)]
struct X11ReplyTimeout {
    operation: String,
}

#[cfg(target_os = "linux")]
impl fmt::Display for X11ReplyTimeout {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} exceeded the {} ms deadline",
            self.operation, X11_REPLY_TIMEOUT_MS
        )
    }
}

#[cfg(target_os = "linux")]
impl std::error::Error for X11ReplyTimeout {}

#[cfg(target_os = "linux")]
fn reply_timeout(operation: &str) -> anyhow::Error {
    anyhow!(X11ReplyTimeout {
        operation: operation.to_owned(),
    })
}

#[cfg(target_os = "linux")]
fn is_reply_timeout(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| cause.is::<X11ReplyTimeout>())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Rect {
    x: i16,
    y: i16,
    width: u16,
    height: u16,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct DisplayFormat {
    depth: u8,
    bits_per_pixel: u8,
    scanline_pad: u8,
    image_lsb_first: bool,
    red_mask: u32,
    green_mask: u32,
    blue_mask: u32,
}

impl DisplayFormat {
    fn validate_bgra(&self) -> Result<()> {
        if !self.image_lsb_first
            || self.bits_per_pixel != 32
            || (self.depth != 24 && self.depth != 32)
            || self.red_mask != 0x00ff_0000
            || self.green_mask != 0x0000_ff00
            || self.blue_mask != 0x0000_00ff
        {
            bail!("X11 root image format is not supported by shared-memory screenshot capture");
        }
        Ok(())
    }
}

/// Validate a root-relative region before it is converted to X11 wire types.
///
/// XShmGetImage accepts signed 16-bit coordinates and unsigned 16-bit
/// dimensions.  Keeping this check in one place prevents truncation and makes
/// the same safety contract apply to full and regional screenshots.
fn validate_rect(
    root_width: u16,
    root_height: u16,
    x: i32,
    y: i32,
    width: usize,
    height: usize,
) -> Result<Rect> {
    if width == 0 || height == 0 {
        bail!("capture dimensions must be positive");
    }
    let x = i16::try_from(x).context("capture x is outside the X11 coordinate range")?;
    let y = i16::try_from(y).context("capture y is outside the X11 coordinate range")?;
    if x < 0 || y < 0 {
        bail!("capture region coordinates must be non-negative");
    }
    let width = u16::try_from(width).context("capture width is outside the X11 range")?;
    let height = u16::try_from(height).context("capture height is outside the X11 range")?;
    let right = (x as u32)
        .checked_add(width as u32)
        .ok_or_else(|| anyhow!("capture x overflows"))?;
    let bottom = (y as u32)
        .checked_add(height as u32)
        .ok_or_else(|| anyhow!("capture y overflows"))?;
    if right > root_width as u32 || bottom > root_height as u32 {
        bail!("capture region is outside the root drawable");
    }
    Ok(Rect {
        x,
        y,
        width,
        height,
    })
}

#[cfg(target_os = "linux")]
#[derive(Debug)]
struct SharedMemorySlot {
    ptr: NonNull<u8>,
    len: usize,
    shmseg: shm::Seg,
}

#[cfg(target_os = "linux")]
impl SharedMemorySlot {
    fn bytes(&self, len: usize) -> Result<&[u8]> {
        if len > self.len {
            bail!("capture buffer length exceeds the shared-memory slot");
        }
        // XShmGetImage is synchronous in this path: wait_for_reply has
        // completed before the bytes are borrowed, so X is no longer writing
        // the slot while PNG encoding reads it.
        Ok(unsafe { slice::from_raw_parts(self.ptr.as_ptr(), len) })
    }
}

#[cfg(target_os = "linux")]
impl Drop for SharedMemorySlot {
    fn drop(&mut self) {
        // The X server owns the fd after a successful AttachFd request.  The
        // process only owns the mapping here; unmap it exactly once.
        unsafe {
            let _ = libc::munmap(self.ptr.as_ptr().cast::<c_void>(), self.len);
        }
    }
}

#[derive(Default)]
struct RgbPngEncoder {
    rgb: Vec<u8>,
}

#[cfg(target_os = "linux")]
impl RgbPngEncoder {
    fn encode(
        &mut self,
        slot: &SharedMemorySlot,
        width: u16,
        height: u16,
        stride: usize,
    ) -> Result<Vec<u8>> {
        let rgb_len = (width as usize)
            .checked_mul(height as usize)
            .and_then(|pixels| pixels.checked_mul(3))
            .ok_or_else(|| anyhow!("RGB buffer size overflows usize"))?;
        let frame_len = stride
            .checked_mul(height as usize)
            .ok_or_else(|| anyhow!("XImage buffer size overflows usize"))?;
        let source = slot.bytes(frame_len)?;
        if stride < (width as usize).saturating_mul(4) {
            bail!("XImage stride is narrower than the requested row");
        }
        if self.rgb.len() != rgb_len {
            self.rgb.resize(rgb_len, 0);
        }
        for (source_row, destination_row) in source
            .chunks_exact(stride)
            .zip(self.rgb.chunks_exact_mut(width as usize * 3))
        {
            convert_bgra_row(source_row, destination_row);
        }

        let mut output = Vec::with_capacity(rgb_len / 2);
        let mut encoder = png::Encoder::new(&mut output, width as u32, height as u32);
        encoder.set_color(png::ColorType::Rgb);
        encoder.set_depth(png::BitDepth::Eight);
        // `Compression::Fast` in png 0.18 selects fdeflate's ultra-fast
        // profile, not zlib level 1.  This final candidate keeps MSS's
        // filter-0 policy while testing a level-two DEFLATE tradeoff; it
        // remains a separate codec and must be validated against live MSS.
        encoder.set_deflate_compression(png::DeflateCompression::Level(2));
        encoder.set_filter(png::Filter::NoFilter);
        let mut writer = encoder.write_header().context("PNG header write failed")?;
        writer
            .write_image_data(&self.rgb)
            .context("PNG image write failed")?;
        writer.finish().context("PNG finish failed")?;
        Ok(output)
    }
}

fn convert_bgra_row(source: &[u8], destination: &mut [u8]) {
    for (source, destination) in source.chunks_exact(4).zip(destination.chunks_exact_mut(3)) {
        destination[0] = source[2];
        destination[1] = source[1];
        destination[2] = source[0];
    }
}

#[cfg(target_os = "linux")]
/// One serialized X11/MIT-SHM capture session.
///
/// The Python wrapper holds the GIL and the daemon serializes screenshot
/// operations, so one slot is sufficient.  Keeping one slot avoids a pool
/// whose only purpose would be to permit concurrent captures that the daemon
/// intentionally does not schedule.
#[cfg_attr(feature = "extension-module", pyclass(unsendable))]
pub struct X11SharedMemoryScreenshotSession {
    conn: Connection,
    root: x::Window,
    root_visual: x::Visualid,
    width: u16,
    height: u16,
    format: DisplayFormat,
    slot: Option<SharedMemorySlot>,
    slot_len: usize,
    encoder: RgbPngEncoder,
    closed: bool,
    connection_failed: bool,
}

#[cfg(target_os = "linux")]
#[derive(Debug, PartialEq, Eq)]
enum PollState {
    Ready(libc::c_short),
    Timeout,
}

#[cfg(target_os = "linux")]
fn poll_timeout_ms(remaining: Duration) -> libc::c_int {
    (remaining.as_nanos().saturating_add(999_999) / 1_000_000).min(i32::MAX as u128) as libc::c_int
}

#[cfg(target_os = "linux")]
fn poll_socket_until_with<F>(
    fd: RawFd,
    events: libc::c_short,
    deadline: Instant,
    mut poll: F,
) -> Result<PollState>
where
    F: FnMut(&mut libc::pollfd, libc::c_int) -> io::Result<libc::c_int>,
{
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Ok(PollState::Timeout);
        }
        // Round up so a sub-millisecond remainder cannot turn into a busy
        // loop of zero-timeout polls while the absolute deadline remains
        // authoritative.
        let timeout_ms = poll_timeout_ms(remaining);
        let mut descriptor = libc::pollfd {
            fd,
            events,
            revents: 0,
        };
        match poll(&mut descriptor, timeout_ms) {
            Ok(0) => return Ok(PollState::Timeout),
            Ok(result) if result > 0 => return Ok(PollState::Ready(descriptor.revents)),
            Ok(result) => {
                return Err(anyhow!("poll returned unexpected result {result}"));
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error.into()),
        }
    }
}

#[cfg(target_os = "linux")]
fn poll_socket_until(fd: RawFd, events: libc::c_short, deadline: Instant) -> Result<PollState> {
    poll_socket_until_with(fd, events, deadline, |descriptor, timeout_ms| {
        let result = unsafe { libc::poll(descriptor, 1, timeout_ms) };
        if result >= 0 {
            Ok(result)
        } else {
            Err(io::Error::last_os_error())
        }
    })
}

#[cfg(target_os = "linux")]
fn poll_socket_error(operation: &str, revents: libc::c_short) -> anyhow::Error {
    anyhow!(
        "{operation} socket became unavailable (poll revents 0x{:x})",
        revents as libc::c_ushort
    )
}

#[cfg(target_os = "linux")]
fn poll_ready_reply<T, E, F>(
    operation: &str,
    revents: libc::c_short,
    deadline: Instant,
    mut poll_for_reply: F,
) -> Result<Option<T>>
where
    E: std::error::Error + Send + Sync + 'static,
    F: FnMut() -> Option<std::result::Result<T, E>>,
{
    if revents & libc::POLLNVAL != 0 {
        return Err(poll_socket_error(operation, revents));
    }
    if Instant::now() >= deadline {
        return Err(reply_timeout(operation));
    }
    if let Some(reply) = poll_for_reply() {
        if Instant::now() >= deadline {
            return Err(reply_timeout(operation));
        }
        return reply
            .map(Some)
            .with_context(|| format!("{operation} reply failed"));
    }
    if revents & (libc::POLLERR | libc::POLLHUP) != 0 {
        return Err(poll_socket_error(operation, revents));
    }
    Ok(None)
}

#[cfg(target_os = "linux")]
fn poll_reply_bounded<C>(conn: &Connection, cookie: &C, operation: &str) -> Result<C::Reply>
where
    C: xcb::CookieWithReplyChecked,
{
    let deadline = Instant::now() + X11_REPLY_TIMEOUT;
    match poll_socket_until(conn.as_raw_fd(), libc::POLLOUT, deadline)
        .with_context(|| format!("{operation} socket poll failed"))?
    {
        PollState::Timeout => return Err(reply_timeout(operation)),
        PollState::Ready(revents)
            if revents & (libc::POLLERR | libc::POLLHUP | libc::POLLNVAL) != 0 =>
        {
            return Err(poll_socket_error(operation, revents));
        }
        PollState::Ready(_) => {}
    }
    if Instant::now() >= deadline {
        return Err(reply_timeout(operation));
    }
    conn.flush()
        .with_context(|| format!("{operation} flush failed"))?;
    if Instant::now() >= deadline {
        return Err(reply_timeout(operation));
    }
    loop {
        if let Some(reply) =
            poll_ready_reply(operation, 0, deadline, || conn.poll_for_reply(cookie))?
        {
            return Ok(reply);
        }
        conn.has_error()
            .with_context(|| format!("{operation} connection failed"))?;
        let poll_state = poll_socket_until(
            conn.as_raw_fd(),
            libc::POLLIN | libc::POLLERR | libc::POLLHUP | libc::POLLNVAL,
            deadline,
        )
        .with_context(|| format!("{operation} socket poll failed"))?;
        match poll_state {
            PollState::Timeout => return Err(reply_timeout(operation)),
            PollState::Ready(revents) => {
                // Give XCB one last chance to drain a reply when readiness
                // arrives together with a terminal error/hangup event.
                if let Some(reply) =
                    poll_ready_reply(operation, revents, deadline, || conn.poll_for_reply(cookie))?
                {
                    return Ok(reply);
                }
                conn.has_error()
                    .with_context(|| format!("{operation} connection failed"))?;
            }
        }
    }
}

#[cfg(target_os = "linux")]
impl X11SharedMemoryScreenshotSession {
    fn connect(display: &str, expected_width: usize, expected_height: usize) -> Result<Self> {
        if expected_width == 0 || expected_height == 0 {
            bail!("capture dimensions must be positive");
        }
        let expected_width =
            u16::try_from(expected_width).context("expected width is outside the X11 range")?;
        let expected_height =
            u16::try_from(expected_height).context("expected height is outside the X11 range")?;
        let (conn, screen_num) =
            Connection::connect(Some(display)).context("unable to connect to DISPLAY")?;
        let send_timeout = libc::timeval {
            tv_sec: 0,
            tv_usec: X11_REPLY_TIMEOUT.as_micros() as libc::suseconds_t,
        };
        let send_timeout_result = unsafe {
            libc::setsockopt(
                conn.as_raw_fd(),
                libc::SOL_SOCKET,
                libc::SO_SNDTIMEO,
                std::ptr::addr_of!(send_timeout).cast(),
                std::mem::size_of::<libc::timeval>() as libc::socklen_t,
            )
        };
        if send_timeout_result != 0 {
            return Err(std::io::Error::last_os_error())
                .context("could not bound X11 socket writes");
        }
        if shm::get_extension_data(&conn).is_none() {
            bail!("server does not advertise MIT-SHM");
        }
        let version_cookie = conn.send_request(&shm::QueryVersion {});
        let version = poll_reply_bounded(&conn, &version_cookie, "MIT-SHM QueryVersion")?;
        if (version.major_version(), version.minor_version()) < (1, 2) {
            bail!("MIT-SHM FD attach requires version >= 1.2");
        }

        let (root, root_visual, width, height, format) = {
            let setup = conn.get_setup();
            let screen = setup
                .roots()
                .nth(screen_num as usize)
                .ok_or_else(|| anyhow!("screen index {screen_num} missing"))?;
            let width = screen.width_in_pixels();
            let height = screen.height_in_pixels();
            if width != expected_width || height != expected_height {
                bail!(
                    "display dimensions {}x{} differ from expected {}x{}",
                    width,
                    height,
                    expected_width,
                    expected_height
                );
            }
            let root_depth = screen.root_depth();
            let root_visual = screen.root_visual();
            let pixmap_format = setup
                .pixmap_formats()
                .iter()
                .find(|format| format.depth() == root_depth)
                .ok_or_else(|| anyhow!("root pixmap format is unavailable"))?;
            let visual = screen
                .allowed_depths()
                .find(|depth| depth.depth() == root_depth)
                .and_then(|depth| {
                    depth
                        .visuals()
                        .iter()
                        .find(|visual| visual.visual_id() == root_visual)
                })
                .ok_or_else(|| anyhow!("root visual is unavailable"))?;
            let format = DisplayFormat {
                depth: root_depth,
                bits_per_pixel: pixmap_format.bits_per_pixel(),
                scanline_pad: pixmap_format.scanline_pad(),
                image_lsb_first: matches!(setup.image_byte_order(), x::ImageOrder::LsbFirst),
                red_mask: visual.red_mask(),
                green_mask: visual.green_mask(),
                blue_mask: visual.blue_mask(),
            };
            format.validate_bgra()?;
            (screen.root(), root_visual, width, height, format)
        };
        let slot_len = row_stride(width, format.bits_per_pixel, format.scanline_pad)?
            .checked_mul(height as usize)
            .ok_or_else(|| anyhow!("XShm segment size overflows usize"))?;
        let slot = create_slot(&conn, slot_len)?;
        Ok(Self {
            conn,
            root,
            root_visual,
            width,
            height,
            format,
            slot: Some(slot),
            slot_len,
            encoder: RgbPngEncoder::default(),
            closed: false,
            connection_failed: false,
        })
    }

    fn capture_region(&mut self, rect: Rect) -> Result<Vec<u8>> {
        let stride = row_stride(
            rect.width,
            self.format.bits_per_pixel,
            self.format.scanline_pad,
        )?;
        let len = stride
            .checked_mul(rect.height as usize)
            .ok_or_else(|| anyhow!("capture region size overflows usize"))?;
        if len > self.slot_len {
            bail!("capture region is larger than the XShm slot");
        }
        let slot = self.slot.as_ref().ok_or_else(|| {
            anyhow!("X11 shared-memory screenshot session has no shared-memory slot")
        })?;
        let cookie = self.conn.send_request(&shm::GetImage {
            drawable: x::Drawable::Window(self.root),
            x: rect.x,
            y: rect.y,
            width: rect.width,
            height: rect.height,
            plane_mask: u32::MAX,
            format: x::ImageFormat::ZPixmap as u8,
            shmseg: slot.shmseg,
            offset: 0,
        });
        let reply = match poll_reply_bounded(&self.conn, &cookie, "XShm GetImage") {
            Ok(reply) => reply,
            Err(error) => {
                self.connection_failed = true;
                return Err(error);
            }
        };
        if reply.depth() != self.format.depth || reply.visual() != self.root_visual {
            bail!("XShm reply visual or depth differs from the validated root");
        }
        let server_len = reply.size() as usize;
        if server_len < len || server_len > slot.len {
            bail!("XShm reply size does not match the validated slot");
        }
        self.encoder.encode(slot, rect.width, rect.height, stride)
    }

    fn capture_png_bytes(
        &mut self,
        x: i32,
        y: i32,
        width: usize,
        height: usize,
    ) -> Result<Vec<u8>> {
        if self.closed {
            bail!("X11 shared-memory screenshot session is closed");
        }
        let rect = validate_rect(self.width, self.height, x, y, width, height)?;
        self.capture_region(rect)
    }

    fn close_inner(&mut self) -> Result<()> {
        if self.closed {
            return Ok(());
        }
        if self.slot.is_none() {
            self.closed = true;
            return Ok(());
        }
        if self.connection_failed {
            // Keep the mapping in the struct. Field drop order disconnects
            // XCB before the slot is unmapped, so a timed-out server cannot
            // retain a pointer into released client memory.
            self.closed = true;
            return Ok(());
        }
        let detach_cookie = self.conn.send_request_checked(&shm::Detach {
            shmseg: self.slot.as_ref().expect("slot checked above").shmseg,
        });
        let fence_cookie = self.conn.send_request(&x::GetInputFocus {});
        if let Err(error) = poll_reply_bounded(&self.conn, &fence_cookie, "XShm detach fence") {
            self.connection_failed = true;
            self.closed = true;
            return Err(error);
        }
        if let Err(error) = self.conn.check_request(detach_cookie) {
            self.connection_failed = true;
            self.closed = true;
            return Err(anyhow!("XShm detach failed: {error:?}"));
        }
        self.closed = true;
        let slot = self.slot.take().expect("slot checked above");
        drop(slot);
        Ok(())
    }
}

#[cfg(not(target_os = "linux"))]
pub struct X11SharedMemoryScreenshotSession;

#[cfg(not(target_os = "linux"))]
impl X11SharedMemoryScreenshotSession {
    fn connect(_display: &str, _expected_width: usize, _expected_height: usize) -> Result<Self> {
        bail!("FD-backed XShm capture is Linux-only")
    }
}

#[cfg(target_os = "linux")]
impl Drop for X11SharedMemoryScreenshotSession {
    fn drop(&mut self) {
        let _ = self.close_inner();
    }
}

#[cfg(target_os = "linux")]
fn create_slot(conn: &Connection, len: usize) -> Result<SharedMemorySlot> {
    if len == 0 {
        bail!("XShm segment size must be positive");
    }
    let name = CString::new("modal-computer-use-x11-shm").expect("static memfd name");
    let fd = unsafe { libc::memfd_create(name.as_ptr(), libc::MFD_CLOEXEC) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error()).context("memfd_create");
    }
    if unsafe { libc::ftruncate(fd, len as libc::off_t) } != 0 {
        let error = std::io::Error::last_os_error();
        unsafe { libc::close(fd) };
        return Err(error).context("ftruncate");
    }
    let mapped = unsafe {
        libc::mmap(
            std::ptr::null_mut(),
            len,
            libc::PROT_READ,
            libc::MAP_SHARED,
            fd,
            0,
        )
    };
    if mapped == libc::MAP_FAILED {
        let error = std::io::Error::last_os_error();
        unsafe { libc::close(fd) };
        return Err(error).context("mmap");
    }
    let Some(ptr) = NonNull::new(mapped.cast::<u8>()) else {
        unsafe {
            let _ = libc::munmap(mapped, len);
            let _ = libc::close(fd);
        }
        bail!("mmap returned a null pointer");
    };
    let shmseg: shm::Seg = conn.generate_id();
    let attach_cookie = conn.send_request_checked(&shm::AttachFd {
        shmseg,
        shm_fd: fd,
        read_only: false,
    });
    // A successful xcb AttachFd request transfers fd ownership to XCB; it
    // closes the descriptor after sending it.  Do not close fd here.  On a
    // protocol error XCB still owns the descriptor and will close it while
    // processing the failed request.  We only release our mapping.
    let fence_cookie = conn.send_request(&x::GetInputFocus {});
    let attach_result =
        poll_reply_bounded(conn, &fence_cookie, "XShm AttachFd fence").and_then(|_| {
            conn.check_request(attach_cookie)
                .map_err(|error| anyhow!("XShm AttachFd failed: {error:?}"))
        });
    if let Err(error) = attach_result {
        unsafe {
            let _ = libc::munmap(ptr.as_ptr().cast::<c_void>(), len);
        }
        return Err(error);
    }
    Ok(SharedMemorySlot { ptr, len, shmseg })
}

#[cfg(not(target_os = "linux"))]
fn row_stride(_width: u16, _bits_per_pixel: u8, _scanline_pad: u8) -> Result<usize> {
    bail!("FD-backed XShm capture is Linux-only")
}

#[cfg(target_os = "linux")]
fn row_stride(width: u16, bits_per_pixel: u8, scanline_pad: u8) -> Result<usize> {
    if scanline_pad == 0 || !scanline_pad.is_multiple_of(8) {
        bail!("invalid XImage scanline padding");
    }
    if bits_per_pixel == 0 {
        bail!("invalid XImage bits per pixel");
    }
    let bits = (width as usize)
        .checked_mul(bits_per_pixel as usize)
        .ok_or_else(|| anyhow!("row bit count overflows usize"))?;
    let pad = scanline_pad as usize;
    Ok(bits
        .checked_add(pad - 1)
        .ok_or_else(|| anyhow!("padded row bit count overflows usize"))?
        / pad
        * pad
        / 8)
}

#[cfg(all(target_os = "linux", feature = "extension-module"))]
#[pymethods]
impl X11SharedMemoryScreenshotSession {
    #[new]
    fn new(display: &str, expected_width: usize, expected_height: usize) -> PyResult<Self> {
        Self::connect(display, expected_width, expected_height).map_err(|error| {
            if is_reply_timeout(&error) {
                X11ScreenshotTimeoutError::new_err(format!(
                    "X11 shared-memory screenshot startup timed out: {error:#}"
                ))
            } else {
                PyRuntimeError::new_err(format!(
                    "X11 shared-memory screenshot startup failed: {error:#}"
                ))
            }
        })
    }

    /// Capture a complete or regional lossless RGB PNG.
    #[pyo3(signature = (x=0, y=0, width=None, height=None))]
    fn capture_png<'py>(
        &mut self,
        py: Python<'py>,
        x: i32,
        y: i32,
        width: Option<usize>,
        height: Option<usize>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let width = width.unwrap_or(self.width as usize);
        let height = height.unwrap_or(self.height as usize);
        let output = self
            .capture_png_bytes(x, y, width, height)
            .map_err(|error| {
                if is_reply_timeout(&error) {
                    X11ScreenshotTimeoutError::new_err(format!(
                        "X11 shared-memory screenshot timed out: {error:#}"
                    ))
                } else {
                    PyValueError::new_err(format!("X11 shared-memory screenshot failed: {error:#}"))
                }
            })?;
        Ok(PyBytes::new(py, &output))
    }

    fn dimensions(&self) -> (usize, usize) {
        (self.width as usize, self.height as usize)
    }

    fn close(&mut self) -> PyResult<()> {
        self.close_inner().map_err(|error| {
            PyRuntimeError::new_err(format!(
                "X11 shared-memory screenshot close failed: {error:#}"
            ))
        })
    }
}

#[cfg(all(target_os = "linux", feature = "extension-module"))]
#[pymodule]
fn _modal_computer_use_x11_shm(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<X11SharedMemoryScreenshotSession>()?;
    module.add(
        "X11ScreenshotTimeoutError",
        py.get_type::<X11ScreenshotTimeoutError>(),
    )?;
    module.add("backend", BACKEND_MARKER)?;
    module.add("codec", CODEC_MARKER)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(target_os = "linux")]
    use std::io::Cursor;
    #[cfg(target_os = "linux")]
    use xcb::Xid;

    #[test]
    fn rectangle_validation_accepts_full_and_region() {
        assert_eq!(
            validate_rect(1024, 768, 0, 0, 1024, 768).unwrap(),
            Rect {
                x: 0,
                y: 0,
                width: 1024,
                height: 768,
            }
        );
        assert_eq!(
            validate_rect(1024, 768, 20, 30, 100, 200).unwrap(),
            Rect {
                x: 20,
                y: 30,
                width: 100,
                height: 200,
            }
        );
    }

    #[test]
    fn rectangle_validation_rejects_truncation_and_out_of_bounds() {
        for (x, y, width, height) in [
            (-1, 0, 1, 1),
            (0, -1, 1, 1),
            (1024, 0, 1, 1),
            (0, 768, 1, 1),
            (1000, 0, 25, 1),
            (0, 750, 1, 25),
            (0, 0, 0, 1),
            (0, 0, 1, 0),
        ] {
            assert!(validate_rect(1024, 768, x, y, width, height).is_err());
        }
        assert!(validate_rect(1024, 768, i16::MAX as i32 + 1, 0, 1, 1).is_err());
        assert!(validate_rect(1024, 768, 0, 0, usize::from(u16::MAX) + 1, 1).is_err());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn row_stride_matches_ximage_padding() {
        assert_eq!(row_stride(1024, 32, 32).unwrap(), 4096);
        assert_eq!(row_stride(3, 24, 32).unwrap(), 12);
        assert_eq!(row_stride(3, 32, 32).unwrap(), 12);
        assert!(row_stride(3, 32, 0).is_err());
        assert!(row_stride(3, 32, 7).is_err());
        assert!(row_stride(3, 0, 32).is_err());
    }

    #[test]
    fn display_format_rejects_non_bgra_visuals() {
        let format = DisplayFormat {
            depth: 24,
            bits_per_pixel: 32,
            scanline_pad: 32,
            image_lsb_first: true,
            red_mask: 0x00ff_0000,
            green_mask: 0x0000_ff00,
            blue_mask: 0x0000_00ff,
        };
        assert!(format.validate_bgra().is_ok());
        assert!(DisplayFormat {
            image_lsb_first: false,
            ..format
        }
        .validate_bgra()
        .is_err());
        assert!(DisplayFormat {
            red_mask: 0x0000_00ff,
            ..format
        }
        .validate_bgra()
        .is_err());
    }

    #[test]
    fn native_markers_keep_backend_and_codec_semantic() {
        assert_eq!(BACKEND_MARKER, "x11-shm");
        assert_eq!(CODEC_MARKER, "png-deflate-level2-no-filter");
    }

    #[test]
    fn x11_reply_budget_keeps_the_preregistered_headroom() {
        assert_eq!(X11_REPLY_TIMEOUT_MS, 750);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn poll_timeout_rounds_up_without_extending_the_deadline() {
        assert_eq!(poll_timeout_ms(Duration::from_nanos(1)), 1);
        assert_eq!(poll_timeout_ms(Duration::from_millis(750)), 750);
        assert_eq!(
            poll_timeout_ms(Duration::from_millis(100) + Duration::from_nanos(1)),
            101
        );
        assert!(poll_timeout_ms(Duration::from_millis(100)) > 0);
    }

    #[cfg(target_os = "linux")]
    fn test_pipe() -> [RawFd; 2] {
        let mut fds = [-1; 2];
        assert_eq!(unsafe { libc::pipe(fds.as_mut_ptr()) }, 0);
        fds
    }

    #[cfg(target_os = "linux")]
    fn close_test_fd(fd: RawFd) {
        assert_eq!(unsafe { libc::close(fd) }, 0);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn socket_poll_wakes_for_readable_pipe() {
        let [read_fd, write_fd] = test_pipe();
        let byte = [1_u8];
        assert_eq!(
            unsafe { libc::write(write_fd, byte.as_ptr().cast(), byte.len()) },
            1
        );
        let state = poll_socket_until(
            read_fd,
            libc::POLLIN,
            Instant::now() + Duration::from_millis(100),
        )
        .unwrap();
        assert!(matches!(
            state,
            PollState::Ready(revents) if revents & libc::POLLIN != 0
        ));
        close_test_fd(read_fd);
        close_test_fd(write_fd);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn socket_poll_returns_timeout_at_deadline() {
        let [read_fd, write_fd] = test_pipe();
        let state = poll_socket_until(
            read_fd,
            libc::POLLIN,
            Instant::now() + Duration::from_millis(20),
        )
        .unwrap();
        assert_eq!(state, PollState::Timeout);
        close_test_fd(read_fd);
        close_test_fd(write_fd);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn socket_poll_reports_hup_and_invalid_fd() {
        let [read_fd, write_fd] = test_pipe();
        close_test_fd(write_fd);
        let state = poll_socket_until(
            read_fd,
            libc::POLLIN | libc::POLLHUP,
            Instant::now() + Duration::from_millis(100),
        )
        .unwrap();
        assert!(matches!(
            state,
            PollState::Ready(revents) if revents & libc::POLLHUP != 0
        ));
        close_test_fd(read_fd);

        let state = poll_socket_until(
            -1,
            libc::POLLIN,
            Instant::now() + Duration::from_millis(100),
        )
        .unwrap();
        assert!(matches!(
            state,
            PollState::Ready(revents) if revents & libc::POLLNVAL != 0
        ));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn socket_poll_retries_interrupted_syscalls() {
        let mut calls = 0;
        let mut max_timeout_ms = 0;
        let state = poll_socket_until_with(
            -1,
            libc::POLLIN,
            Instant::now() + Duration::from_millis(100),
            |descriptor, timeout_ms| {
                calls += 1;
                max_timeout_ms = max_timeout_ms.max(timeout_ms);
                if calls == 1 {
                    Err(io::Error::from_raw_os_error(libc::EINTR))
                } else {
                    descriptor.revents = libc::POLLIN;
                    Ok(1)
                }
            },
        )
        .unwrap();
        assert_eq!(calls, 2);
        assert!(max_timeout_ms > 0 && max_timeout_ms <= 100);
        assert_eq!(state, PollState::Ready(libc::POLLIN));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn ready_reply_is_drained_before_hup_error() {
        let mut calls = 0;
        let reply = poll_ready_reply(
            "test reply",
            libc::POLLHUP,
            Instant::now() + Duration::from_millis(100),
            || {
                calls += 1;
                Some(Ok::<u8, io::Error>(7_u8))
            },
        )
        .unwrap();
        assert_eq!(calls, 1);
        assert_eq!(reply, Some(7));

        let error = poll_ready_reply(
            "test reply",
            libc::POLLHUP,
            Instant::now() + Duration::from_millis(100),
            || None::<std::result::Result<u8, io::Error>>,
        )
        .unwrap_err();
        assert!(error.to_string().contains("socket became unavailable"));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn ready_reply_does_not_cross_the_deadline() {
        let error = poll_ready_reply(
            "expired reply",
            0,
            Instant::now() - Duration::from_millis(1),
            || Some(Ok::<u8, io::Error>(7_u8)),
        )
        .unwrap_err();
        assert!(is_reply_timeout(&error));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn socket_poll_propagates_non_interrupted_errors() {
        let error = poll_socket_until_with(
            -1,
            libc::POLLIN,
            Instant::now() + Duration::from_millis(100),
            |_descriptor, _timeout_ms| Err(io::Error::from_raw_os_error(libc::EBADF)),
        )
        .unwrap_err();
        assert!(error.chain().any(|cause| cause.is::<io::Error>()));
    }

    #[test]
    fn bgra_conversion_ignores_padding_and_preserves_rgb_order() {
        let source = [10, 20, 30, 255, 40, 50, 60, 255, 99, 98, 97, 96];
        let mut destination = [0_u8; 6];
        convert_bgra_row(&source, &mut destination);
        assert_eq!(destination, [30, 20, 10, 60, 50, 40]);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn png_scratch_round_trips_decoded_pixels() {
        let pixels = [
            10_u8, 20, 30, 255, // RGB 30, 20, 10
            40, 50, 60, 255, // RGB 60, 50, 40
        ];
        let ptr = std::ptr::NonNull::new(pixels.as_ptr() as *mut u8).unwrap();
        // The test slot only borrows a static buffer; forget its Drop mapping
        // behavior by using ManuallyDrop so no munmap is attempted.
        let slot = std::mem::ManuallyDrop::new(SharedMemorySlot {
            ptr,
            len: pixels.len(),
            shmseg: xcb::shm::Seg::none(),
        });
        let mut scratch = RgbPngEncoder::default();
        let output = scratch.encode(&slot, 2, 1, 8).unwrap();
        let decoder = png::Decoder::new(Cursor::new(output));
        let mut reader = decoder.read_info().unwrap();
        let mut decoded = vec![0_u8; reader.output_buffer_size().unwrap()];
        let info = reader.next_frame(&mut decoded).unwrap();
        assert_eq!(info.width, 2);
        assert_eq!(info.height, 1);
        assert_eq!(&decoded[..info.buffer_size()], &[30, 20, 10, 60, 50, 40]);
    }

    fn representative_browser_rgb(width: u16, height: u16) -> Vec<u8> {
        let width = width as usize;
        let height = height as usize;
        let mut rgb = vec![0_u8; width * height * 3];
        for y in 0..height {
            for x in 0..width {
                let pixel = if y < 56 {
                    let control = (x / 32) % 6 == 0 && (8..40).contains(&(y % 48));
                    if control {
                        (103, 148, 214)
                    } else {
                        (31, 42, 58)
                    }
                } else if (96..height.saturating_sub(32)).contains(&y) && ((x / 256) % 3 != 2) {
                    let card_y = y % 128;
                    let card_x = x % 256;
                    let text_run = (16..24).contains(&card_y)
                        || (42..49).contains(&card_y)
                        || (66..71).contains(&card_y);
                    if text_run && (24..220).contains(&card_x) {
                        (64, 72, 86)
                    } else if (8..244).contains(&card_x) {
                        (
                            (x.wrapping_mul(3) + y.wrapping_mul(2)) as u8,
                            (x.wrapping_mul(5) + y.wrapping_mul(3)) as u8,
                            (x.wrapping_mul(7) + y.wrapping_mul(5)) as u8,
                        )
                    } else {
                        (250, 251, 253)
                    }
                } else if (x / 64 + y / 32) % 2 == 0 {
                    (232, 236, 242)
                } else {
                    (244, 247, 250)
                };
                let offset = (y * width + x) * 3;
                rgb[offset..offset + 3].copy_from_slice(&[pixel.0, pixel.1, pixel.2]);
            }
        }
        rgb
    }

    #[cfg(target_os = "linux")]
    fn decode_rgb_png(encoded: &[u8]) -> (u32, u32, Vec<u8>) {
        let decoder = png::Decoder::new(Cursor::new(encoded));
        let mut reader = decoder.read_info().unwrap();
        let mut decoded = vec![0_u8; reader.output_buffer_size().unwrap()];
        let info = reader.next_frame(&mut decoded).unwrap();
        decoded.truncate(info.buffer_size());
        (info.width, info.height, decoded)
    }

    #[cfg(target_os = "linux")]
    fn encode_native_rgb(rgb: &[u8], width: u16, height: u16) -> Vec<u8> {
        let mut bgra = vec![0_u8; width as usize * height as usize * 4];
        for (rgb_pixel, bgra_pixel) in rgb.chunks_exact(3).zip(bgra.chunks_exact_mut(4)) {
            bgra_pixel.copy_from_slice(&[rgb_pixel[2], rgb_pixel[1], rgb_pixel[0], 255]);
        }
        let ptr = std::ptr::NonNull::new(bgra.as_ptr() as *mut u8).unwrap();
        let slot = std::mem::ManuallyDrop::new(SharedMemorySlot {
            ptr,
            len: bgra.len(),
            shmseg: xcb::shm::Seg::none(),
        });
        let mut encoder = RgbPngEncoder::default();
        encoder
            .encode(&slot, width, height, width as usize * 4)
            .unwrap()
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn no_filter_preserves_rgb_and_matches_reference() {
        for (width, height) in [(1024_u16, 768_u16), (511_u16, 383_u16)] {
            let rgb = representative_browser_rgb(width, height);
            let native = encode_native_rgb(&rgb, width, height);
            let reference = encode_reference_png(&rgb, width, height, png::Filter::NoFilter, 2);
            let (native_width, native_height, native_rgb) = decode_rgb_png(&native);
            assert_eq!(
                (native_width, native_height, native_rgb),
                (u32::from(width), u32::from(height), rgb),
                "Level2 NoFilter PNG changed decoded RGB for {width}x{height}"
            );
            assert_eq!(
                native.len(),
                reference.len(),
                "native Level2 NoFilter PNG differs from the reference payload for {width}x{height}"
            );
        }
    }

    fn encode_reference_png(
        rgb: &[u8],
        width: u16,
        height: u16,
        filter: png::Filter,
        level: u8,
    ) -> Vec<u8> {
        let mut output = Vec::new();
        let mut encoder = png::Encoder::new(&mut output, width as u32, height as u32);
        encoder.set_color(png::ColorType::Rgb);
        encoder.set_depth(png::BitDepth::Eight);
        encoder.set_deflate_compression(png::DeflateCompression::Level(level));
        encoder.set_filter(filter);
        let mut writer = encoder.write_header().unwrap();
        writer.write_image_data(rgb).unwrap();
        writer.finish().unwrap();
        output
    }

    #[test]
    fn no_filter_level_two_reference_is_smaller_than_level_one() {
        for (width, height) in [(1024_u16, 768_u16), (511_u16, 383_u16)] {
            let rgb = representative_browser_rgb(width, height);
            let level_one = encode_reference_png(&rgb, width, height, png::Filter::NoFilter, 1);
            let level_two = encode_reference_png(&rgb, width, height, png::Filter::NoFilter, 2);
            assert!(
                level_two.len() < level_one.len(),
                "Level2 NoFilter payload {} is not below Level1 {} for {width}x{height}",
                level_two.len(),
                level_one.len()
            );
        }
    }
}
