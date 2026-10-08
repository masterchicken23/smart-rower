#!/usr/bin/env python3
"""
Frame sources. Each backend yields Frame(jpeg, t_ms, ...), where `t_ms` is the
capture instant on `backend.clock` -- the clock __main__ then stamps
`t_send_ms` on, so the pair is coherent (SENDER.md).

  hw    picamera2 + the VideoCore MJPEG encoder (V4L2 M2M). The CPU never
        touches pixels, which is the only way an original Pi Zero W (ARMv6,
        one core, no NEON) holds 720p at 15 fps. Greyscale is free: the ISP is
        told Saturation=0, so the chroma planes are flat and compress to
        almost nothing.
  sw    picamera2 frames JPEG-encoded on the CPU with simplejpeg, from the
        YUV planes directly (greyscale: the Y plane alone), so there is no
        colour conversion. Exact per-frame timing and a quality servo, but a
        Zero W cannot hold 720p15 this way; a Zero 2 W can.
  uvc   USB webcam: its own MJPEG frames passed through, decimated to --fps.
  test  synthetic frames, software-paced. No camera; for checking the whole
        chain to the detector on any machine.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from . import clock as clk

NO_FRAMES_WARN_S = 3.0


def log(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


@dataclass
class Frame:
    jpeg: bytes
    t_ms: float
    """Capture instant, ms, on the backend's clock."""
    skipped: int = 0
    """Frames the camera produced since the previous Frame that were never
    yielded. The sender spends a seq on each, so the gap shows downstream."""
    enc_ms: Optional[float] = None
    """CPU encode time, where there is one to measure."""
    extra: dict[str, Any] = field(default_factory=dict)
    """Carried in the header untouched (exposure_us, gain)."""


class QualityController:
    """Servo the JPEG quality factor toward a byte budget.

    From experiments/network-video-yolo/cam-sender/pi_sender.py. Scene content
    changes the compressed size far more than the quality factor does, so a
    fixed quality gives a wildly variable bitrate. With a budget set, quality
    moves +/-1 per frame (2 when far off) outside a +/-12% deadband, which
    tracks scene changes within a few frames without visible pumping."""

    def __init__(self, quality: int, target_bytes: Optional[int],
                 qmin: int, qmax: int) -> None:
        self.q = quality
        self.target = target_bytes
        self.qmin, self.qmax = qmin, qmax

    def update(self, nbytes: int) -> None:
        if not self.target:
            return
        if nbytes > self.target * 1.12 and self.q > self.qmin:
            self.q -= 2 if nbytes > self.target * 1.5 else 1
        elif nbytes < self.target * 0.88 and self.q < self.qmax:
            self.q += 2 if nbytes < self.target * 0.6 else 1
        self.q = max(self.qmin, min(self.qmax, self.q))


class Backend:
    name = ""

    def __init__(self, args: Any, stop: threading.Event) -> None:
        self.args = args
        self.stop = stop
        self.clock: clk.Clock = clk.PERF
        self.ts_source = "dequeue"
        """Where `t_ms` comes from: "sensor/<clock>" or "dequeue"."""
        self.qc: Optional[QualityController] = None

    def frames(self) -> Iterator[Frame]:
        raise NotImplementedError

    def _use_sensor_clock(self, candidates: list[tuple[str, Optional[int]]]) -> str:
        """Pick the first (label, sensor_ns) whose timestamp lands on a kernel
        clock, and adopt that clock. Returns the label, or "dequeue" if none
        did -- then t_ms is the dequeue time on the fallback clock."""
        for label, ns in candidates:
            if ns is None:
                continue
            c = clk.detect(ns)
            if c is not None:
                self.clock = c
                self.ts_source = f"sensor/{c.name}"
                log(f"[clock] capture time from the sensor timestamp ({label}), "
                    f"on CLOCK_{c.name.upper()}")
                return label
        self.clock = clk.PERF
        self.ts_source = "dequeue"
        log("[clock] WARNING: sensor timestamp matches no kernel clock; using "
            "the dequeue time instead (ms.enc and ms.e2e will read low)")
        return "dequeue"


# --------------------------------------------------------------------------- #
# picamera2 backends
# --------------------------------------------------------------------------- #
def _picamera2() -> Any:
    try:
        from picamera2 import Picamera2
    except ImportError as e:
        raise SystemExit(
            f"picamera2 is not importable ({e}).\n"
            "  sudo apt install -y python3-picamera2 python3-zmq python3-simplejpeg\n"
            "and run with the system python3, or from a venv created with "
            "--system-site-packages. --backend test needs no camera."
        ) from None
    return Picamera2


def _transform(args: Any) -> Any:
    from libcamera import Transform

    return Transform(hflip=int(args.hflip), vflip=int(args.vflip))


def _configure(args: Any, controls: dict[str, Any], fmt: str = "YUV420") -> Any:
    Picamera2 = _picamera2()
    picam2 = Picamera2()
    dur = int(round(1_000_000 / args.fps))
    # The sensor paces the stream: a fixed frame duration is the frame rate.
    # picamera2 rejects any control the camera does not advertise, and USB
    # (UVC) cameras advertise few, FrameDurationLimits not among them.
    want = {"FrameDurationLimits": (dur, dur), **controls}
    have = picam2.camera_controls
    dropped = sorted(k for k in want if k not in have)
    if dropped:
        log(f"[cam] camera does not support {', '.join(dropped)}; not set")
    cfg = picam2.create_video_configuration(
        main={"size": (args.width, args.height), "format": fmt},
        controls={k: v for k, v in want.items() if k in have},
        transform=_transform(args),
    )
    picam2.configure(cfg)
    main = picam2.camera_config["main"]
    if main["format"] != fmt:
        picam2.close()
        hint = " (a USB webcam: use --backend uvc)" if main["format"] == "MJPEG" else ""
        raise SystemExit(f"[cam] --backend {args.backend} needs {fmt} frames, "
                         f"camera gives {main['format']}{hint}")
    got = tuple(main["size"])
    if got != (args.width, args.height):
        log(f"[cam] WARNING: asked for {args.width}x{args.height}, camera gives "
            f"{got[0]}x{got[1]}")
    return picam2


class HwBackend(Backend):
    """GPU MJPEG. picamera2's encoder thread hands each JPEG to a one-slot
    mailbox; the main thread takes the newest and sends it. A frame replaced
    in the mailbox before it was taken counts as skipped."""

    name = "hw"

    def frames(self) -> Iterator[Frame]:
        a = self.args
        picam2 = _configure(a, {} if a.color else {"Saturation": 0.0})

        from picamera2.encoders import MJPEGEncoder
        from picamera2.outputs import Output

        bitrate = int(a.target_kb * 1024 * 8 * a.fps) if a.target_kb else None
        encoder = MJPEGEncoder(bitrate=bitrate) if bitrate else MJPEGEncoder()

        cv = threading.Condition()
        slot: list[tuple[bytes, Optional[int], int]] = []
        skipped = 0

        class Mailbox(Output):
            # Signature varies across picamera2 releases; only the first three
            # arguments matter here.
            def outputframe(self, frame: Any, keyframe: bool = True,
                            timestamp: Optional[int] = None, *_a: Any,
                            **_k: Any) -> None:
                nonlocal skipped
                t_deq = clk.PERF.now_ns()
                data = bytes(frame)            # the buffer is reused; copy now
                with cv:
                    if slot:
                        slot.clear()
                        skipped += 1
                    slot.append((data, timestamp, t_deq))
                    cv.notify()

        picam2.start_recording(encoder, Mailbox())
        log(f"[cam] hw: picamera2 + VideoCore MJPEG {a.width}x{a.height}"
            f"@{a.fps:g} {'colour' if a.color else 'grey'}, bitrate "
            + (f"{bitrate / 1e6:.2f} Mbit/s" if bitrate else "encoder default"))

        mode: Optional[str] = None
        last = time.monotonic()
        try:
            while not self.stop.is_set():
                with cv:
                    if not slot:
                        cv.wait(timeout=1.0)
                    if not slot:
                        if time.monotonic() - last > NO_FRAMES_WARN_S:
                            log("[cam] no frames from the encoder for "
                                f"{time.monotonic() - last:.0f}s")
                            last = time.monotonic()
                        continue
                    data, ts_us, t_deq = slot.pop()
                    n_skip, skipped = skipped, 0
                last = time.monotonic()

                # picamera2 passes the sensor timestamp in us *relative to the
                # first frame*, and keeps the base on the encoder; older
                # releases passed it absolute. Try both, once.
                if mode is None:
                    first = getattr(encoder, "firsttimestamp", None)
                    mode = self._use_sensor_clock([
                        ("relative+base", (ts_us + first) * 1000
                         if ts_us is not None and first is not None else None),
                        ("absolute", ts_us * 1000 if ts_us is not None else None),
                    ])
                if mode == "relative+base":
                    t_ms = (ts_us + encoder.firsttimestamp) / 1000.0
                elif mode == "absolute":
                    t_ms = ts_us / 1000.0
                else:
                    t_ms = t_deq / 1e6
                yield Frame(data, t_ms, skipped=n_skip)
        finally:
            try:
                picam2.stop_recording()
            finally:
                picam2.close()


class SwBackend(Backend):
    """CPU JPEG from the YUV planes, with exact per-frame metadata."""

    name = "sw"

    def frames(self) -> Iterator[Frame]:
        import numpy as np

        try:
            import simplejpeg
        except ImportError:
            raise SystemExit("--backend sw needs simplejpeg: "
                             "sudo apt install -y python3-simplejpeg") from None

        a = self.args
        w, h = a.width, a.height
        self.qc = QualityController(
            a.quality, int(a.target_kb * 1024) if a.target_kb else None,
            a.qmin, a.qmax)
        picam2 = _configure(a, {})
        picam2.start()
        log(f"[cam] sw: picamera2 + simplejpeg {w}x{h}@{a.fps:g} "
            f"{'colour' if a.color else 'grey'}, quality {self.qc.q}"
            + (f" servoed to {a.target_kb:g} KB" if self.qc.target else ""))

        period_ns = 1e9 / a.fps
        prev_ns: Optional[int] = None
        sensor = True
        try:
            while not self.stop.is_set():
                req = picam2.capture_request()
                t_deq = clk.PERF.now_ns()
                try:
                    md = req.get_metadata()
                    arr = req.make_array("main")
                finally:
                    req.release()

                ts = md.get("SensorTimestamp")
                if prev_ns is None:
                    sensor = self._use_sensor_clock([("SensorTimestamp", ts)]) != "dequeue"
                t_ns = ts if sensor and ts is not None else t_deq
                # Frames the camera captured while we were encoding are
                # recycled by libcamera unseen; count them from the time gap.
                n_skip = 0
                if prev_ns is not None:
                    n_skip = max(0, round((t_ns - prev_ns) / period_ns) - 1)
                prev_ns = t_ns

                # I420 at row stride s: Y is h rows of s, then U and V are
                # h/2 rows of s/2 each.
                s = arr.shape[1]
                flat = arr.reshape(-1)
                y = flat[:s * h].reshape(h, s)[:, :w]
                t0 = time.perf_counter()
                if a.color:
                    cs, n = s // 2, (s // 2) * (h // 2)
                    u = flat[s * h:s * h + n].reshape(h // 2, cs)[:, :w // 2]
                    v = flat[s * h + n:s * h + 2 * n].reshape(h // 2, cs)[:, :w // 2]
                    jpeg = simplejpeg.encode_jpeg_yuv_planes(
                        np.ascontiguousarray(y), np.ascontiguousarray(u),
                        np.ascontiguousarray(v), quality=self.qc.q)
                else:
                    jpeg = simplejpeg.encode_jpeg(
                        np.ascontiguousarray(y)[:, :, None], quality=self.qc.q,
                        colorspace="GRAY")
                enc_ms = (time.perf_counter() - t0) * 1000.0
                self.qc.update(len(jpeg))

                extra: dict[str, Any] = {}
                if "ExposureTime" in md:
                    extra["exposure_us"] = md["ExposureTime"]
                if "AnalogueGain" in md:
                    extra["gain"] = round(float(md["AnalogueGain"]), 3)
                yield Frame(jpeg, t_ns / 1e6, skipped=n_skip, enc_ms=enc_ms,
                            extra=extra)
        finally:
            try:
                picam2.stop()
            finally:
                picam2.close()


class UvcBackend(Backend):
    """USB webcam that delivers MJPEG: its JPEGs are sent as they come, so
    nothing is encoded at all. UVC offers no frame-duration control, so the
    camera runs at its mode's rate and frames are dropped here down to --fps;
    the drops count as skipped. JPEG size is the camera's own (--target-kb and
    --quality do not apply), and greyscale is Saturation=0 where the camera
    supports it."""

    name = "uvc"

    def frames(self) -> Iterator[Frame]:
        a = self.args
        picam2 = _configure(a, {} if a.color else {"Saturation": 0.0}, "MJPEG")
        picam2.start()
        log(f"[cam] uvc: camera MJPEG passed through, {a.width}x{a.height}"
            f" decimated to <= {a.fps:g} fps")

        period_ns = 1e9 / a.fps
        next_ns: Optional[float] = None
        n_skip = 0
        sensor = True
        try:
            while not self.stop.is_set():
                req = picam2.capture_request()
                t_deq = clk.PERF.now_ns()
                try:
                    md = req.get_metadata()
                    # picamera2 maps bytes_used, so this is the JPEG alone.
                    jpeg = req.make_buffer("main").tobytes()
                finally:
                    req.release()

                ts = md.get("SensorTimestamp")
                if next_ns is None:
                    sensor = self._use_sensor_clock([("SensorTimestamp", ts)]) != "dequeue"
                t_ns = ts if sensor and ts is not None else t_deq

                # A quarter-period of slack absorbs USB timestamp jitter
                # without letting two frames through in one period.
                if next_ns is not None and t_ns < next_ns - period_ns / 4:
                    n_skip += 1
                    continue
                next_ns = (t_ns + period_ns
                           if next_ns is None or t_ns > next_ns + period_ns
                           else next_ns + period_ns)
                if not jpeg.startswith(b"\xff\xd8"):
                    n_skip += 1                    # truncated USB transfer
                    continue
                yield Frame(jpeg, t_ns / 1e6, skipped=n_skip)
                n_skip = 0
        finally:
            try:
                picam2.stop()
            finally:
                picam2.close()


# --------------------------------------------------------------------------- #
class SyntheticBackend(Backend):
    """A gradient with a black square crossing it. Paced in software, timed on
    perf_counter like tools/fake_camera.py. JPEG via simplejpeg if installed,
    else OpenCV."""

    name = "test"

    def frames(self) -> Iterator[Frame]:
        import numpy as np

        a = self.args
        w, h = a.width, a.height
        encode = _test_encoder(a.color)
        self.qc = QualityController(
            a.quality, int(a.target_kb * 1024) if a.target_kb else None,
            a.qmin, a.qmax)

        yy, xx = np.mgrid[0:h, 0:w]
        base = ((xx * 160 // w + yy * 80 // h) + 40).astype(np.uint8)
        rng = np.random.default_rng(0)
        base = np.clip(base.astype(np.int16)
                       + rng.integers(-6, 7, (h, w)), 0, 255).astype(np.uint8)
        side = max(8, h // 6)
        self.ts_source = "generated"
        log(f"[cam] test: synthetic {w}x{h}@{a.fps:g} "
            f"{'colour' if a.color else 'grey'}")

        period = 1.0 / a.fps
        next_t = time.perf_counter()
        i = 0
        while not self.stop.is_set():
            t_ms = self.clock.now_ms()                 # "capture"
            img = base.copy()
            x = (i * 7) % (w - side)
            y0 = (h - side) // 2
            img[y0:y0 + side, x:x + side] = 0
            if a.color:
                img = np.dstack([img, np.roll(img, i, axis=1), 255 - img])
            t0 = time.perf_counter()
            jpeg = encode(img, self.qc.q)
            enc_ms = (time.perf_counter() - t0) * 1000.0
            self.qc.update(len(jpeg))
            yield Frame(jpeg, t_ms, enc_ms=enc_ms)
            i += 1

            next_t += period
            slack = next_t - time.perf_counter()
            if slack > 0:
                self.stop.wait(slack)
            else:
                next_t = time.perf_counter()          # overran: do not burst


def _test_encoder(color: bool) -> Any:
    try:
        import simplejpeg

        cs = "BGR" if color else "GRAY"

        def enc(img: Any, q: int) -> bytes:
            return simplejpeg.encode_jpeg(img if color else img[:, :, None],
                                          quality=q, colorspace=cs)
        return enc
    except ImportError:
        pass
    try:
        import cv2
    except ImportError:
        raise SystemExit("--backend test needs simplejpeg or opencv: "
                         "pip install simplejpeg") from None

    def enc_cv(img: Any, q: int) -> bytes:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
        if not ok:
            raise RuntimeError("cv2.imencode failed")
        return buf.tobytes()
    return enc_cv


BACKENDS: dict[str, type[Backend]] = {
    "hw": HwBackend, "sw": SwBackend, "uvc": UvcBackend, "test": SyntheticBackend}


def create(args: Any, stop: threading.Event) -> Backend:
    return BACKENDS[args.backend](args, stop)
