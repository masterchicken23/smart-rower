#!/usr/bin/env python3
"""
Minimal V4L2 MJPEG capture, pure Python (ctypes + ioctl + mmap), for the uvc
backend.

libcamera's uvcvideo pipeline handler fails STREAMON with EPROTO on some
webcams (the Innomaker U20CAM, 0c45:6366, on a Pi Zero W) that stream fine
through plain V4L2, so USB cameras are read here directly, the same way
`v4l2-ctl --stream-mmap` does. Nothing beyond the standard library, so no apt
package is needed for it.

The struct layouts follow linux/videodev2.h. ctypes lays them out with the
native alignment, which matches the kernel's on 64-bit and on 32-bit ARM with
64-bit time_t (Raspberry Pi OS trixie and later): the timestamp is declared
as two int64s, the kernel's own __kernel_v4l2_timeval. ioctl numbers encode
the struct size, so a mismatch fails with ENOTTY rather than corrupting.
"""

from __future__ import annotations

import ctypes as C
import errno
import mmap
import os
import select
from typing import Optional

# --------------------------------------------------------------------------- #
# videodev2.h
# --------------------------------------------------------------------------- #
BUF_TYPE_VIDEO_CAPTURE = 1
MEMORY_MMAP = 1
FIELD_ANY = 0
CAP_VIDEO_CAPTURE = 0x00000001
CAP_STREAMING = 0x04000000
CAP_DEVICE_CAPS = 0x80000000
BUF_FLAG_ERROR = 0x00000040
CTRL_FLAG_DISABLED = 0x0001

CID_BASE = 0x00980900
CID_SATURATION = CID_BASE + 2
CID_HFLIP = CID_BASE + 20
CID_VFLIP = CID_BASE + 21


def fourcc(s: str) -> int:
    return sum(ord(c) << (8 * i) for i, c in enumerate(s))


MJPEG_FOURCCS = (fourcc("MJPG"), fourcc("JPEG"))


class Capability(C.Structure):
    _fields_ = [("driver", C.c_char * 16), ("card", C.c_char * 32),
                ("bus_info", C.c_char * 32), ("version", C.c_uint32),
                ("capabilities", C.c_uint32), ("device_caps", C.c_uint32),
                ("reserved", C.c_uint32 * 3)]


class PixFormat(C.Structure):
    _fields_ = [("width", C.c_uint32), ("height", C.c_uint32),
                ("pixelformat", C.c_uint32), ("field", C.c_uint32),
                ("bytesperline", C.c_uint32), ("sizeimage", C.c_uint32),
                ("colorspace", C.c_uint32), ("priv", C.c_uint32),
                ("flags", C.c_uint32), ("ycbcr_enc", C.c_uint32),
                ("quantization", C.c_uint32), ("xfer_func", C.c_uint32)]


class _FormatUnion(C.Union):
    # v4l2_window holds pointers, so the union is pointer-aligned.
    _fields_ = [("pix", PixFormat), ("raw_data", C.c_uint8 * 200),
                ("_align", C.c_void_p)]


class Format(C.Structure):
    _fields_ = [("type", C.c_uint32), ("fmt", _FormatUnion)]


class Fract(C.Structure):
    _fields_ = [("numerator", C.c_uint32), ("denominator", C.c_uint32)]


class CaptureParm(C.Structure):
    _fields_ = [("capability", C.c_uint32), ("capturemode", C.c_uint32),
                ("timeperframe", Fract), ("extendedmode", C.c_uint32),
                ("readbuffers", C.c_uint32), ("reserved", C.c_uint32 * 4)]


class _ParmUnion(C.Union):
    _fields_ = [("capture", CaptureParm), ("raw_data", C.c_uint8 * 200)]


class StreamParm(C.Structure):
    _fields_ = [("type", C.c_uint32), ("parm", _ParmUnion)]


class RequestBuffers(C.Structure):
    _fields_ = [("count", C.c_uint32), ("type", C.c_uint32),
                ("memory", C.c_uint32), ("capabilities", C.c_uint32),
                ("flags", C.c_uint8), ("reserved", C.c_uint8 * 3)]


class Timeval(C.Structure):
    _fields_ = [("tv_sec", C.c_int64), ("tv_usec", C.c_int64)]


class Timecode(C.Structure):
    _fields_ = [("type", C.c_uint32), ("flags", C.c_uint32),
                ("frames", C.c_uint8), ("seconds", C.c_uint8),
                ("minutes", C.c_uint8), ("hours", C.c_uint8),
                ("userbits", C.c_uint8 * 4)]


class _BufM(C.Union):
    _fields_ = [("offset", C.c_uint32), ("userptr", C.c_void_p),
                ("planes", C.c_void_p), ("fd", C.c_int32)]


class Buffer(C.Structure):
    _fields_ = [("index", C.c_uint32), ("type", C.c_uint32),
                ("bytesused", C.c_uint32), ("flags", C.c_uint32),
                ("field", C.c_uint32), ("timestamp", Timeval),
                ("timecode", Timecode), ("sequence", C.c_uint32),
                ("memory", C.c_uint32), ("m", _BufM), ("length", C.c_uint32),
                ("reserved2", C.c_uint32), ("request_fd", C.c_int32)]


class QueryCtrl(C.Structure):
    _fields_ = [("id", C.c_uint32), ("type", C.c_uint32),
                ("name", C.c_char * 32), ("minimum", C.c_int32),
                ("maximum", C.c_int32), ("step", C.c_int32),
                ("default_value", C.c_int32), ("flags", C.c_uint32),
                ("reserved", C.c_uint32 * 2)]


class Control(C.Structure):
    _fields_ = [("id", C.c_uint32), ("value", C.c_int32)]


def _ioc(direction: int, nr: int, struct: type) -> int:
    return (direction << 30) | (C.sizeof(struct) << 16) | (ord("V") << 8) | nr


_W, _R = 1, 2
VIDIOC_QUERYCAP = _ioc(_R, 0, Capability)
VIDIOC_S_FMT = _ioc(_R | _W, 5, Format)
VIDIOC_REQBUFS = _ioc(_R | _W, 8, RequestBuffers)
VIDIOC_QUERYBUF = _ioc(_R | _W, 9, Buffer)
VIDIOC_QBUF = _ioc(_R | _W, 15, Buffer)
VIDIOC_DQBUF = _ioc(_R | _W, 17, Buffer)
VIDIOC_STREAMON = _ioc(_W, 18, C.c_int)
VIDIOC_STREAMOFF = _ioc(_W, 19, C.c_int)
VIDIOC_S_PARM = _ioc(_R | _W, 22, StreamParm)
VIDIOC_S_CTRL = _ioc(_R | _W, 28, Control)
VIDIOC_QUERYCTRL = _ioc(_R | _W, 36, QueryCtrl)


# --------------------------------------------------------------------------- #
class Capture:
    """An MJPEG stream from one V4L2 device. Configure on construction,
    `start()`, then `read()` until done; use as a context manager.

    After construction `width`, `height` and `fps` are what the driver
    actually chose, which may differ from what was asked."""

    def __init__(self, path: str, width: int, height: int, fps: float,
                 nbuf: int = 4) -> None:
        import fcntl

        self._ioctl = fcntl.ioctl
        self.path = path
        self.nbuf = nbuf
        self._maps: list[mmap.mmap] = []
        self._streaming = False
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            self._setup(width, height, fps)
        except BaseException:
            os.close(self.fd)
            raise

    def _setup(self, width: int, height: int, fps: float) -> None:
        cap = Capability()
        self._ioctl(self.fd, VIDIOC_QUERYCAP, cap)
        caps = cap.device_caps if cap.capabilities & CAP_DEVICE_CAPS else cap.capabilities
        if not (caps & CAP_VIDEO_CAPTURE and caps & CAP_STREAMING):
            raise OSError(errno.ENODEV, "not a streaming capture device")
        self.card = cap.card.decode(errors="replace")

        f = Format(type=BUF_TYPE_VIDEO_CAPTURE)
        f.fmt.pix.width, f.fmt.pix.height = width, height
        f.fmt.pix.pixelformat = MJPEG_FOURCCS[0]
        f.fmt.pix.field = FIELD_ANY
        self._ioctl(self.fd, VIDIOC_S_FMT, f)
        if f.fmt.pix.pixelformat not in MJPEG_FOURCCS:
            raise OSError(errno.EINVAL, "camera offers no MJPEG format")
        self.width, self.height = f.fmt.pix.width, f.fmt.pix.height

        # Ask for the frame rate; the driver rounds to the nearest interval
        # the mode has (often 30 fps only). Not every driver supports it.
        p = StreamParm(type=BUF_TYPE_VIDEO_CAPTURE)
        tpf = p.parm.capture.timeperframe
        tpf.numerator, tpf.denominator = 1000, max(1, round(fps * 1000))
        self.fps: Optional[float] = None
        try:
            self._ioctl(self.fd, VIDIOC_S_PARM, p)
            if tpf.numerator and tpf.denominator:
                self.fps = tpf.denominator / tpf.numerator
        except OSError:
            pass

    def ctrl_range(self, cid: int) -> Optional[tuple[int, int]]:
        """(min, max) of a control, or None if the device lacks it."""
        q = QueryCtrl(id=cid)
        try:
            self._ioctl(self.fd, VIDIOC_QUERYCTRL, q)
        except OSError:
            return None
        if q.flags & CTRL_FLAG_DISABLED:
            return None
        return q.minimum, q.maximum

    def set_ctrl(self, cid: int, value: int) -> None:
        self._ioctl(self.fd, VIDIOC_S_CTRL, Control(id=cid, value=value))

    def start(self) -> None:
        rb = RequestBuffers(count=self.nbuf, type=BUF_TYPE_VIDEO_CAPTURE,
                            memory=MEMORY_MMAP)
        self._ioctl(self.fd, VIDIOC_REQBUFS, rb)
        for i in range(rb.count):
            b = Buffer(index=i, type=BUF_TYPE_VIDEO_CAPTURE, memory=MEMORY_MMAP)
            self._ioctl(self.fd, VIDIOC_QUERYBUF, b)
            self._maps.append(mmap.mmap(self.fd, b.length, mmap.MAP_SHARED,
                                        mmap.PROT_READ | mmap.PROT_WRITE,
                                        offset=b.m.offset))
            self._ioctl(self.fd, VIDIOC_QBUF, b)
        self._ioctl(self.fd, VIDIOC_STREAMON, C.c_int(BUF_TYPE_VIDEO_CAPTURE))
        self._streaming = True

    def read(self, timeout: float) -> Optional[tuple[bytes, int, int, bool]]:
        """Next frame as (jpeg, timestamp_ns, sequence, error), or None if
        none arrived within `timeout` s. `timestamp_ns` is the driver's
        capture time (CLOCK_MONOTONIC for uvcvideo); `sequence` counts every
        frame the device produced, so gaps are frames the driver dropped;
        `error` is set on frames the driver flagged as corrupt."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        b = Buffer(type=BUF_TYPE_VIDEO_CAPTURE, memory=MEMORY_MMAP)
        try:
            self._ioctl(self.fd, VIDIOC_DQBUF, b)
        except BlockingIOError:
            return None
        try:
            data = self._maps[b.index][:b.bytesused]
        finally:
            self._ioctl(self.fd, VIDIOC_QBUF, b)
        ts = b.timestamp.tv_sec * 1_000_000_000 + b.timestamp.tv_usec * 1000
        return data, ts, b.sequence, bool(b.flags & BUF_FLAG_ERROR)

    def close(self) -> None:
        try:
            if self._streaming:
                self._ioctl(self.fd, VIDIOC_STREAMOFF,
                            C.c_int(BUF_TYPE_VIDEO_CAPTURE))
                self._streaming = False
        finally:
            for m in self._maps:
                m.close()
            self._maps.clear()
            os.close(self.fd)

    def __enter__(self) -> "Capture":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
