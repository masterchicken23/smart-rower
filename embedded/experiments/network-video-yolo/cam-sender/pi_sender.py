#!/usr/bin/env python3
"""
Raspberry Pi Zero -> imageZMQ JPEG publisher.

Standalone counterpart to pose_stream.py, but it shares nothing with it: it just
speaks the imageZMQ wire format, so any imageZMQ receiver works.

  camera -> JPEG encode -> imagezmq PUB (bind)  ...  SUB (connect) on the Jetson

Topology note: in imageZMQ PUB/SUB the *sender binds* and the receiver connects.
So this script binds (--bind tcp://*:5555) and the Jetson runs
  pose_stream.py --connect tcp://<pi-ip>:5555
PUB also drops frames at the high-water mark instead of blocking, so a slow or
absent subscriber can never stall the camera loop. --mode reqrep is available for
imageZMQ's REQ/REP topology, where this script connects instead.

Compression is configurable three ways (pick one):
  --quality 75        fixed JPEG quality factor
  --ratio 40          target 40:1 vs. raw 24bpp; quality is servoed to hit it
  --target-kb 24      target bytes/frame; quality is servoed to hit it
The achieved ratio and quality are reported in the stderr stats line.

Camera backends (--backend auto picks one; auto prefers legacy picamera on ARMv6,
i.e. the original Pi Zero / Zero W, because that path encodes on the GPU):
  picamera   legacy MMAL stack. JPEG comes out of the GPU encoder -- by far the
             cheapest option on a single-core Zero W; the CPU never sees a bitmap.
  picamera2  libcamera stack (Zero 2 W, Bookworm). Software JPEG via simplejpeg or
             OpenCV, unless --hw-jpeg is set to use the V4L2 MJPEG hardware encoder.
  v4l2       any UVC/V4L2 camera via OpenCV, MJPG fourcc passed through uncompressed
             where the driver supports it.
  dummy      synthetic frames, no camera. For testing the link end to end.

Requires: imagezmq, pyzmq. Plus one of: picamera / picamera2 / opencv-python.
simplejpeg is optional but ~2x faster than cv2 for the picamera2 software path.
"""

from __future__ import annotations

import argparse
import io
import os
import platform
import signal
import socket
import sys
import threading
import time
from typing import Iterator, Optional

STOP = threading.Event()


def log(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


def preflight() -> None:
    """Turn the two import failures that actually bite on a fresh Pi into advice.

    numpy is unavoidable here (imagezmq imports it at module scope), and the
    pip/piwheels numpy wheel links against the system OpenBLAS, which Raspberry
    Pi OS does not install. The result is a 40-line C-extension traceback that
    looks like a numpy bug and is not one.
    """
    try:
        import numpy  # noqa: F401
    except ImportError as e:
        detail = str(e)
        if "libopenblas" in detail or "cannot open shared object" in detail:
            raise SystemExit(
                "numpy is installed but cannot load its C extensions:\n"
                "  libopenblas.so.0: cannot open shared object file\n\n"
                "The pip/piwheels numpy wheel links against the system OpenBLAS,\n"
                "which Raspberry Pi OS does not install by default. Either:\n\n"
                "  sudo apt install -y libopenblas0\n\n"
                "or drop the pip numpy and use the packaged one, which is built\n"
                "against the right libraries:\n\n"
                "  pip uninstall -y numpy && sudo apt install -y python3-numpy\n"
                "  (the venv must have been created with --system-site-packages)"
            )
        raise SystemExit(f"numpy failed to import: {e}")
    try:
        import zmq  # noqa: F401
    except ImportError:
        raise SystemExit("pyzmq is missing:  pip install pyzmq")


# --------------------------------------------------------------------------- #
# compression control
# --------------------------------------------------------------------------- #
class QualityController:
    """Servo the JPEG quality factor toward a byte budget.

    Scene content changes the compressed size far more than the quality factor
    does, so a fixed quality gives a wildly variable bitrate. When a budget is
    set we nudge quality by +/-1 per frame (deadband +/-12%), which tracks scene
    changes within a few frames without visible pumping.
    """

    def __init__(self, quality: int, target_bytes: Optional[int],
                 qmin: int, qmax: int) -> None:
        self.q = quality
        self.target = target_bytes
        self.qmin, self.qmax = qmin, qmax
        self.adaptive = target_bytes is not None

    def update(self, nbytes: int) -> None:
        if not self.adaptive:
            return
        lo, hi = self.target * 0.88, self.target * 1.12
        if nbytes > hi and self.q > self.qmin:
            self.q -= 2 if nbytes > self.target * 1.5 else 1
        elif nbytes < lo and self.q < self.qmax:
            self.q += 2 if nbytes < self.target * 0.6 else 1
        self.q = max(self.qmin, min(self.qmax, self.q))


def target_bytes_from(args) -> Optional[int]:
    raw = args.width * args.height * 3  # 24bpp reference for the ratio
    if args.target_kb:
        return int(args.target_kb * 1024)
    if args.ratio:
        return int(raw / args.ratio)
    return None


# --------------------------------------------------------------------------- #
# encoders (software paths)
# --------------------------------------------------------------------------- #
def make_encoder(args):
    """Return encode(bgr_array, quality) -> bytes."""
    try:
        import simplejpeg

        def enc(arr, q):
            return simplejpeg.encode_jpeg(arr, quality=q, colorspace="BGR",
                                          colorsubsampling=args.subsampling)
        log("[enc] simplejpeg")
        return enc
    except ImportError:
        import cv2

        cv2.setNumThreads(args.cv_threads)
        params = [cv2.IMWRITE_JPEG_QUALITY, 0]
        if args.subsampling == "420":
            params += [cv2.IMWRITE_JPEG_SAMPLING_FACTOR,
                       cv2.IMWRITE_JPEG_SAMPLING_FACTOR_420]
        elif args.subsampling == "444":
            params += [cv2.IMWRITE_JPEG_SAMPLING_FACTOR,
                       cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444]

        def enc(arr, q):
            params[1] = q
            ok, buf = cv2.imencode(".jpg", arr, params)
            if not ok:
                raise RuntimeError("cv2.imencode failed")
            return buf.tobytes()
        log("[enc] opencv (install simplejpeg for ~2x faster encode)")
        return enc


# --------------------------------------------------------------------------- #
# camera backends -- each yields (jpeg_bytes, encode_ms, raw_bytes)
# --------------------------------------------------------------------------- #
HINTS = {
    "picamera2": "sudo apt install -y python3-picamera2   (venv needs "
                 "--system-site-packages)",
    "rpicam": "sudo apt install -y rpicam-apps        (no Python camera "
              "library needed)",
    "v4l2": "sudo apt install -y python3-opencv",
    "picamera": "legacy stack only; removed from Raspberry Pi OS Bookworm, "
                "no python3-picamera package exists there",
}


def probe_backends(rpicam_bin: str = "") -> "list[tuple[str, bool, str]]":
    """(name, usable, reason) for every backend, without starting a camera."""
    import shutil

    out = []
    for name, mod in (("picamera2", "picamera2"), ("picamera", "picamera"),
                      ("v4l2", "cv2"), ("dummy", "cv2")):
        try:
            __import__(mod)
            out.append((name, True, f"{mod} importable"))
        except Exception as e:  # noqa: BLE001
            out.append((name, False, f"{type(e).__name__}: {e}"))
    exe = (rpicam_bin or shutil.which("rpicam-vid")
           or shutil.which("libcamera-vid"))
    out.append(("rpicam", bool(exe), exe or "rpicam-vid/libcamera-vid not on PATH"))
    return out


def candidates(args) -> "list[str]":
    if args.backend != "auto":
        return [args.backend]
    usable = {n for n, ok, _ in probe_backends(args.rpicam_bin) if ok}
    # picamera2 first: it is the only backend that can reach the V4L2 hardware
    # JPEG encoder. rpicam next because it needs no Python camera library.
    # Legacy picamera last -- it only works on the retired camera stack, and an
    # importable module is no guarantee the stack is actually enabled.
    order = ["picamera2", "rpicam", "v4l2", "picamera"]
    return [b for b in order if b in usable]


def no_backend_error(tried, rpicam_bin: str = "") -> str:
    lines = ["no working camera backend.", ""]
    if tried:
        lines.append("Tried, in order:")
        for name, why in tried:
            lines.append(f"  {name:<10} {why}")
        lines.append("")
    lines.append("Probe:")
    for name, ok, why in probe_backends(rpicam_bin):
        lines.append(f"  {name:<10} {'OK  ' if ok else 'no  '} {why}")
    lines += [
        "",
        "Raspberry Pi OS Bookworm removed the legacy camera stack, so there is no",
        "python3-picamera package to install any more. Install one of these:",
        "",
    ] + [f"  {HINTS[k]}" for k in ("picamera2", "rpicam", "v4l2")] + [
        "",
        "Then confirm the camera itself is detected:",
        "  rpicam-hello --list-cameras",
    ]
    return "\n".join(lines)


def start_stream(args, qc: QualityController):
    """Try each candidate until one actually yields a frame.

    An importable module is not proof of a working camera (the legacy stack can
    be disabled, the ribbon can be unseated), so each candidate is verified by
    pulling a real frame before it is accepted.
    """
    import itertools

    cands = candidates(args)
    if not cands:
        raise SystemExit(no_backend_error([], args.rpicam_bin))
    tried = []
    for name in cands:
        gen = BACKENDS[name](args, qc)
        try:
            first = next(gen)
        except StopIteration:
            if STOP.is_set():
                raise SystemExit(0)
            tried.append((name, "started but produced no frames"))
        except Exception as e:  # noqa: BLE001
            tried.append((name, f"{type(e).__name__}: {e}"))
        else:
            log(f"[cam] backend: {name}")
            return name, itertools.chain([first], gen)
        try:
            gen.close()
        except Exception:  # noqa: BLE001
            pass
        log(f"[cam] {name} unusable ({tried[-1][1]}), trying next")
    raise SystemExit(no_backend_error(tried, args.rpicam_bin))


def gen_picamera(args, qc: QualityController) -> Iterator[tuple]:
    """Legacy MMAL stack: the GPU encodes, the CPU only moves bytes."""
    import picamera

    raw = args.width * args.height * 3
    with picamera.PiCamera(resolution=(args.width, args.height),
                           framerate=args.fps) as cam:
        cam.rotation = args.rotation
        cam.hflip, cam.vflip = args.hflip, args.vflip
        log(f"[cam] picamera (GPU JPEG) {args.width}x{args.height}@{args.fps}, "
            f"warming up {args.warmup}s")
        time.sleep(args.warmup)
        stream = io.BytesIO()

        if not qc.adaptive:
            # quality is fixed at generator creation, so the fast path can stay
            # inside capture_continuous with no per-frame setup
            for _ in cam.capture_continuous(stream, format="jpeg", quality=qc.q,
                                            use_video_port=True):
                if STOP.is_set():
                    break
                t0 = time.monotonic()
                data = stream.getvalue()
                stream.seek(0)
                stream.truncate()
                yield data, (time.monotonic() - t0) * 1000.0, raw
        else:
            # adaptive: capture one at a time so quality can change per frame
            while not STOP.is_set():
                t0 = time.monotonic()
                cam.capture(stream, format="jpeg", quality=qc.q,
                            use_video_port=True)
                data = stream.getvalue()
                stream.seek(0)
                stream.truncate()
                yield data, (time.monotonic() - t0) * 1000.0, raw


def gen_picamera2(args, qc: QualityController) -> Iterator[tuple]:
    from picamera2 import Picamera2

    raw = args.width * args.height * 3
    picam2 = Picamera2()
    dur = int(1_000_000 / args.fps)
    # "RGB888" hands back BGR-ordered numpy, which is what cv2/simplejpeg want
    cfg = picam2.create_video_configuration(
        main={"size": (args.width, args.height), "format": "RGB888"},
        controls={"FrameDurationLimits": (dur, dur)},
        buffer_count=args.buffers,
        transform=_transform(args),
    )
    picam2.configure(cfg)

    if args.hw_jpeg:
        yield from _picamera2_hw(args, qc, picam2, raw)
        return

    encode = make_encoder(args)
    picam2.start()
    log(f"[cam] picamera2 (software JPEG) {args.width}x{args.height}@{args.fps}")
    time.sleep(args.warmup)
    try:
        while not STOP.is_set():
            arr = picam2.capture_array("main")
            t0 = time.monotonic()
            data = encode(arr, qc.q)
            yield data, (time.monotonic() - t0) * 1000.0, arr.nbytes or raw
    finally:
        picam2.stop()


def _picamera2_hw(args, qc: QualityController, picam2, raw: int) -> Iterator[tuple]:
    """V4L2 hardware MJPEG encoder. Rate-controlled by bitrate, not by a quality
    factor, so the byte budget maps straight onto bitrate and --quality does not
    apply. Available on the Zero 2 W; not on the original Zero."""
    from picamera2.encoders import MJPEGEncoder
    from picamera2.outputs import FileOutput

    budget = qc.target or int(raw / 30)
    bitrate = int(budget * 8 * args.fps)

    frames: list = []
    cv = threading.Condition()

    class Sink(io.BufferedIOBase):
        def write(self, buf):  # called on the encoder's thread, one JPEG per call
            with cv:
                if frames:
                    frames.clear()  # keep only the newest; never queue
                frames.append(bytes(buf))
                cv.notify()
            return len(buf)

    picam2.start_recording(MJPEGEncoder(bitrate=bitrate), FileOutput(Sink()))
    log(f"[cam] picamera2 (V4L2 hardware MJPEG) {args.width}x{args.height}"
        f"@{args.fps}, bitrate={bitrate/1e6:.2f} Mbit/s")
    try:
        while not STOP.is_set():
            with cv:
                if not frames and not cv.wait(timeout=1.0):
                    continue
                data = frames.pop() if frames else None
            if data:
                yield data, 0.0, raw  # encode happens off-CPU; no time to charge
    finally:
        picam2.stop_recording()


def _transform(args):
    try:
        from libcamera import Transform

        return Transform(hflip=int(args.hflip), vflip=int(args.vflip))
    except Exception:  # noqa: BLE001
        return None


def gen_rpicam(args, qc: QualityController) -> Iterator[tuple]:
    """Read an MJPEG stream from the rpicam-vid/libcamera-vid CLI.

    Needs no Python camera library at all -- just the rpicam-apps package that
    current Raspberry Pi OS installs by default. That makes it the fallback that
    works when picamera2 is unavailable or too heavy for a 512MB Zero.

    Caveat: rpicam-apps' MjpegEncoder is libjpeg on the CPU, not the VideoCore
    block, and ARMv6 has no NEON for libjpeg-turbo's fast paths -- so on an
    original Zero W expect this to be slower than picamera2 --hw-jpeg. Drop to
    --width 480 --height 320 if you cannot hold the frame rate.
    """
    import select
    import shutil
    import subprocess

    exe = args.rpicam_bin or shutil.which("rpicam-vid") or shutil.which("libcamera-vid")
    if not exe:
        raise RuntimeError("rpicam-vid/libcamera-vid not on PATH "
                           "(sudo apt install -y rpicam-apps)")
    if args.rotation in (90, 270):
        log(f"[cam] rpicam-vid cannot rotate {args.rotation} deg; ignoring "
            f"(only 0/180 are supported)")

    raw = args.width * args.height * 3

    def spawn(q: int):
        cmd = [exe, "-t", "0", "--codec", "mjpeg", "--nopreview", "--flush",
               "--width", str(args.width), "--height", str(args.height),
               "--framerate", str(args.fps), "--quality", str(q), "-o", "-"]
        if args.hflip:
            cmd.append("--hflip")
        if args.vflip:
            cmd.append("--vflip")
        if args.rotation == 180:
            cmd += ["--rotation", "180"]
        log("[cam] " + " ".join(cmd))
        return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)

    proc = spawn(qc.q)
    q_running = qc.q
    t_tune = time.monotonic()
    buf = bytearray()
    try:
        while not STOP.is_set():
            # quality is fixed at process launch, so servoing means relaunching
            # the camera. Rate-limited and deadbanded so it happens rarely.
            if (qc.adaptive and abs(qc.q - q_running) >= 4
                    and time.monotonic() - t_tune >= args.rpicam_retune_s):
                log(f"[cam] retuning quality {q_running} -> {qc.q} "
                    f"(restarts the camera, brief gap)")
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                proc = spawn(qc.q)
                q_running, t_tune = qc.q, time.monotonic()
                buf.clear()

            # bounded wait so STOP is honoured promptly on SIGTERM
            if not select.select([proc.stdout], [], [], 0.5)[0]:
                if proc.poll() is not None:
                    if STOP.is_set():
                        return  # signalled: the child got the same signal we did
                    raise RuntimeError(f"{os.path.basename(exe)} exited "
                                       f"rc={proc.returncode}")
                continue
            chunk = proc.stdout.read(65536)
            if not chunk:
                if STOP.is_set():
                    return
                raise RuntimeError(f"{os.path.basename(exe)} closed its output "
                                   f"(rc={proc.poll()}); is the camera detected? "
                                   f"try: rpicam-hello --list-cameras")
            buf += chunk

            # split the MJPEG stream on SOI/EOI. 0xFF bytes inside entropy-coded
            # data are byte-stuffed as FF00, so a bare FFD9 is a real frame end.
            while True:
                i = buf.find(b"\xff\xd8\xff")
                if i < 0:
                    buf.clear()
                    break
                j = buf.find(b"\xff\xd9", i + 3)
                if j < 0:
                    del buf[:i]  # keep the partial frame, drop anything before it
                    break
                jpg = bytes(buf[i:j + 2])
                del buf[:j + 2]
                yield jpg, 0.0, raw  # encode happens in the child; nothing to time
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


def gen_v4l2(args, qc: QualityController) -> Iterator[tuple]:
    import cv2

    cv2.setNumThreads(args.cv_threads)
    cap = cv2.VideoCapture(args.device_index, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    cap.set(cv2.CAP_PROP_FPS, args.fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not cap.isOpened():
        raise SystemExit(f"cannot open v4l2 device {args.device_index}")
    encode = make_encoder(args)
    raw = args.width * args.height * 3
    log(f"[cam] v4l2 device {args.device_index} {args.width}x{args.height}@{args.fps}")
    try:
        while not STOP.is_set():
            ok, frame = cap.read()
            if not ok:
                log("[cam] read failed")
                STOP.wait(0.1)
                continue
            t0 = time.monotonic()
            data = encode(frame, qc.q)
            yield data, (time.monotonic() - t0) * 1000.0, frame.nbytes or raw
    finally:
        cap.release()


def gen_dummy(args, qc: QualityController) -> Iterator[tuple]:
    """Synthetic moving scene, so JPEG sizes are realistic without a camera."""
    import numpy as np

    encode = make_encoder(args)
    h, w = args.height, args.width
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = np.stack([xx / w * 255, yy / h * 255, np.zeros_like(xx)], -1)
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 40, (h, w, 3), dtype=np.uint8)  # keeps it compressible-ish
    log(f"[cam] dummy generator {w}x{h}@{args.fps}")
    i = 0
    while not STOP.is_set():
        i += 1
        frame = ((base + (i * 7 % 255)) % 255).astype(np.uint8)
        frame = (frame // 2 + noise // 2)
        t0 = time.monotonic()
        data = encode(frame, qc.q)
        yield data, (time.monotonic() - t0) * 1000.0, frame.nbytes


BACKENDS = {"picamera": gen_picamera, "picamera2": gen_picamera2,
            "rpicam": gen_rpicam, "v4l2": gen_v4l2, "dummy": gen_dummy}


# --------------------------------------------------------------------------- #
def make_sender(args):
    import imagezmq
    import zmq

    req_rep = args.mode == "reqrep"
    addr = args.connect if req_rep else args.bind
    sender = imagezmq.ImageSender(connect_to=addr, REQ_REP=req_rep)
    sender.zmq_socket.setsockopt(zmq.LINGER, 0)  # teardown must not block
    if req_rep:
        # REQ blocks until the receiver replies. Without a timeout a dead or
        # unstarted receiver wedges the camera loop permanently (and makes the
        # process unkillable by SIGTERM, since the handler can't interrupt recv).
        sender.zmq_socket.setsockopt(zmq.RCVTIMEO, args.reply_timeout_ms)
    else:
        # PUB drops at the high-water mark rather than blocking, so a stalled
        # subscriber costs us frames, never latency. Applies to subscribers that
        # connect after this point, which is all of them (we bind first).
        sender.zmq_socket.setsockopt(zmq.SNDHWM, args.sndhwm)
        sender.zmq_socket.setsockopt(zmq.LINGER, 0)
    log(f"[net] {'REQ/REP connect' if req_rep else 'PUB bind'} {addr} as '{args.name}'")
    return sender


def main(argv=None) -> int:
    import zmq

    args = parse_args(argv)

    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ.setdefault(var, str(args.cv_threads))
    if args.nice:
        try:
            os.nice(args.nice)
        except OSError as e:
            log(f"[main] nice({args.nice}) failed: {e}")

    preflight()

    if args.list_backends:
        for name, ok, why in probe_backends(args.rpicam_bin):
            print(f"{name:<10} {'OK ' if ok else 'no '} {why}")
            if not ok and name in HINTS:
                print(f"{'':<14}{HINTS[name]}")
        return 0

    def on_signal(signum, _frame):
        log(f"[main] signal {signum}, stopping")
        STOP.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    target = target_bytes_from(args)
    qc = QualityController(args.quality, target, args.qmin, args.qmax)
    if target:
        raw = args.width * args.height * 3
        log(f"[enc] target {target/1024:.1f} KB/frame ({raw/target:.1f}:1), "
            f"quality servoed in [{args.qmin},{args.qmax}], start {qc.q}")
    else:
        log(f"[enc] fixed quality {qc.q}")

    sender = make_sender(args)
    _, frames = start_stream(args, qc)

    rc = 0
    period = 1.0 / args.fps if args.pace else 0.0
    next_t = time.monotonic()
    n = nbytes = 0
    enc_ms = 0.0
    raw_sum = 0
    t_stat = time.monotonic()

    try:
        for data, ms, raw in frames:
            if STOP.is_set():
                break
            qc.update(len(data))
            try:
                sender.send_jpg(args.name, data)
            except zmq.Again:
                # REQ/REP only: reply timed out. REQ enforces strict send/recv
                # alternation, so the socket is unusable now -- rebuild it.
                log(f"[net] no reply within {args.reply_timeout_ms}ms; "
                    f"rebuilding REQ socket")
                try:
                    sender.close()
                except Exception:  # noqa: BLE001
                    pass
                if STOP.wait(0.2):
                    break
                sender = make_sender(args)
                continue
            except Exception as e:  # noqa: BLE001
                if STOP.is_set():
                    break
                log(f"[net] send failed: {e!r}")
                STOP.wait(0.2)
                continue

            n += 1
            nbytes += len(data)
            raw_sum += raw
            enc_ms += ms

            if period:
                # only used when the backend is free-running (dummy / some v4l2
                # drivers); the Pi camera paths are already paced by the sensor
                next_t += period
                slack = next_t - time.monotonic()
                if slack > 0:
                    if STOP.wait(slack):
                        break
                else:
                    next_t = time.monotonic()

            now = time.monotonic()
            if args.stats_interval and now - t_stat >= args.stats_interval:
                dt = now - t_stat
                log(f"[stats] {n/dt:5.1f} fps  {nbytes/n/1024:6.1f} KB/frame  "
                    f"{raw_sum/max(nbytes,1):5.1f}:1  q={qc.q:3d}  "
                    f"enc {enc_ms/n:5.1f} ms  {nbytes*8/dt/1e6:5.2f} Mbit/s")
                n = nbytes = raw_sum = 0
                enc_ms = 0.0
                t_stat = now
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:
        if not STOP.is_set():
            log(f"[cam] stream failed: {e}")
            rc = 1
    finally:
        STOP.set()
        try:
            sender.close()
        except Exception:  # noqa: BLE001
            pass
    log("[main] done")
    return rc


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Pi Zero camera -> JPEG -> imageZMQ publisher.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    n = p.add_argument_group("network")
    n.add_argument("--bind", default="tcp://*:5555",
                   help="PUB bind address (imageZMQ PUB/SUB: the sender binds)")
    n.add_argument("--connect", default="tcp://127.0.0.1:5555",
                   help="REQ/REP receiver address (only used with --mode reqrep)")
    n.add_argument("--mode", choices=("pubsub", "reqrep"), default="pubsub")
    n.add_argument("--name", default=socket.gethostname(),
                   help="sender id sent with each frame")
    n.add_argument("--reply-timeout-ms", type=int, default=2000,
                   help="REQ/REP only: give up on a reply after this and rebuild "
                        "the socket, so a dead receiver can't wedge the camera loop")
    n.add_argument("--sndhwm", type=int, default=2,
                   help="PUB send high-water mark; frames over this are dropped")

    c = p.add_argument_group("camera")
    c.add_argument("--backend", choices=("auto", "picamera", "picamera2", "rpicam",
                                         "v4l2", "dummy"), default="auto",
                   help="auto tries picamera2, rpicam, v4l2, picamera in that "
                        "order and verifies each actually yields a frame")
    c.add_argument("--list-backends", action="store_true",
                   help="report which camera backends are available, and exit")
    c.add_argument("--rpicam-bin", default="",
                   help="path to rpicam-vid/libcamera-vid (default: search PATH)")
    c.add_argument("--rpicam-retune-s", type=float, default=15.0,
                   help="rpicam backend: minimum seconds between quality "
                        "retunes, each of which restarts the camera")
    c.add_argument("--width", type=int, default=640)
    c.add_argument("--height", type=int, default=480)
    c.add_argument("--fps", type=float, default=15.0)
    c.add_argument("--rotation", type=int, default=0, choices=(0, 90, 180, 270))
    c.add_argument("--hflip", action="store_true")
    c.add_argument("--vflip", action="store_true")
    c.add_argument("--warmup", type=float, default=2.0,
                   help="seconds for AWB/AE to settle before the first frame")
    c.add_argument("--buffers", type=int, default=4, help="picamera2 buffer count")
    c.add_argument("--device-index", type=int, default=0, help="v4l2 device index")
    c.add_argument("--pace", action="store_true",
                   help="rate-limit in software (only needed for free-running "
                        "backends; the Pi camera paths are paced by the sensor)")

    j = p.add_argument_group("compression")
    j.add_argument("--quality", type=int, default=75, help="fixed JPEG quality 1-100")
    j.add_argument("--ratio", type=float, default=0.0,
                   help="target compression ratio vs raw 24bpp, e.g. 40 for 40:1; "
                        "servoes quality to hit it")
    j.add_argument("--target-kb", type=float, default=0.0,
                   help="target KB/frame; servoes quality to hit it")
    j.add_argument("--qmin", type=int, default=15, help="quality floor when servoing")
    j.add_argument("--qmax", type=int, default=92, help="quality ceiling when servoing")
    j.add_argument("--subsampling", choices=("420", "422", "444"), default="420",
                   help="chroma subsampling; 420 is smallest, 444 sharpest color")
    j.add_argument("--hw-jpeg", action="store_true",
                   help="picamera2 only: use the V4L2 hardware MJPEG encoder "
                        "(bitrate-controlled, so --quality does not apply)")

    m = p.add_argument_group("misc")
    m.add_argument("--cv-threads", type=int, default=1,
                   help="OpenCV threads; 1 is right for a single-core Zero")
    m.add_argument("--nice", type=int, default=0, help="renice self (negative needs root)")
    m.add_argument("--stats-interval", type=float, default=5.0, help="0 to disable")
    args = p.parse_args(argv)
    if args.ratio and args.target_kb:
        p.error("--ratio and --target-kb are mutually exclusive")
    return args


if __name__ == "__main__":
    sys.exit(main())

