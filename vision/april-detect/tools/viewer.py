#!/usr/bin/env python3
"""
Watch what a detector sees: the camera's video with its detections drawn on,
served to a browser as MJPEG.

Two SUB sockets -- one on the camera (cam-sender, fake_camera.py, anything
SENDER.md describes), one on a detector's `apriltag` PUB -- paired frame to
record by the camera's `seq`, which the detector passes through. Headless on
purpose: the container image has opencv-python-headless and the Jetson has no
monitor, so it runs next to the detectors and the picture goes to a laptop.

    python tools/viewer.py --frames tcp://cam1.local:5555 --tags tcp://127.0.0.1:5561
    # then open http://<this-host>:8080/          (/snapshot.jpg for one frame)

The frame is undistorted with the camera model from `apriltag_meta`, because
`corners_px` are corrected-image pixels (README, "Output contract"); `--raw`
shows the lens's own picture instead and distorts the overlay to match.

HUD states: SYNCED (record drawn on the exact frame it came from), UNSYNCED
(a sender with no `seq`, or the frame was already gone: latest record on the
latest frame), NO DETECTOR (frames but no records for a second; the video is
still shown, so this doubles as a plain camera viewer).

The output is sized for a laptop on Wi-Fi: half-size, JPEG q60, at most
10 fps, ~1-2 Mbit/s (the HUD's `out` figure). `--scale 1 --quality 80
--max-fps 0` is full fidelity. Each client's socket buffer is kept to a couple
of frames, so a slow link drops frames instead of falling further behind.

Subscribing to a camera directly makes the Pi send every frame twice over
Wi-Fi while the viewer runs. Debug tool; don't leave it running.
"""

from __future__ import annotations

import argparse
import json
import signal
import socket
import sys
import threading
import time
from collections import OrderedDict, defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from april_detect import wire  # noqa: E402
from april_detect.calib import Calibration  # noqa: E402

PENDING_S = 0.5
"""How long a record waits for its frame before being drawn unsynced."""
NO_DETECTOR_S = 1.0
SNDBUF = 256 * 1024
"""Per-client socket send buffer: a couple of frames. Left to autotune, Linux
grows it to megabytes, i.e. seconds of video queued on a slow link."""

OK = (0, 255, 255)          # BGR yellow
WARN = (0, 140, 255)        # orange: weak decode or ambiguous rotation
AMBIGUOUS = 0.8


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---- pure helpers ----------------------------------------------------------- #
class FrameBuffer:
    """The last `size` frames by the camera's `seq`. Keys are exact, so seq
    wrapping at 2**32 needs no special handling."""

    def __init__(self, size: int = 60) -> None:
        self.size = size
        self._d: OrderedDict[int, Any] = OrderedDict()

    def put(self, seq: int, item: Any) -> None:
        self._d[seq] = item
        self._d.move_to_end(seq)
        while len(self._d) > self.size:
            self._d.popitem(last=False)

    def get(self, seq: Optional[int]) -> Any:
        return None if seq is None else self._d.get(seq)

    def __len__(self) -> int:
        return len(self._d)


def quat_to_R(q) -> np.ndarray:
    """[w, x, y, z] -> 3x3, the record's convention."""
    w, x, y, z = (float(v) for v in q)
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def quat_to_rvec(q) -> np.ndarray:
    import cv2

    rvec, _ = cv2.Rodrigues(quat_to_R(q))
    return rvec.reshape(3)


def scale_points(px, src: tuple[int, int], dst: tuple[int, int]) -> np.ndarray:
    """Pixel coordinates at resolution `src` -> `dst`, with the pixel-centre
    convention calib.Calibration.scaled_to uses for K."""
    p = np.asarray(px, np.float64).reshape(-1, 2)
    if src == dst:
        return p.copy()
    s = np.array([dst[0] / src[0], dst[1] / src[1]])
    return (p + 0.5) * s - 0.5


def display_size(src: tuple[int, int], scale: float) -> tuple[int, int]:
    """The size a `src`-sized frame is shown at."""
    if scale >= 1:
        return src
    return max(1, round(src[0] * scale)), max(1, round(src[1] * scale))


def _scaled_K(K: np.ndarray, src: tuple[int, int], dst: tuple[int, int]) -> np.ndarray:
    return Calibration(np.asarray(K, float), np.zeros(0), size=src).scaled_to(*dst).K


class Projector:
    """Everything that depends on the camera model and the displayed frame's
    size: the remap that corrects the frame, where a record's corrected-image
    corners land on it, and how a 3-D point in the camera frame projects.

    Built from the detector's meta. In the default (corrected) view the remap
    targets the detector's own `K_rect`, so the picture is the one the detector
    detected on, whatever undistort alpha it was configured with.
    """

    def __init__(self, meta: Optional[dict], size: tuple[int, int],
                 raw: bool = False) -> None:
        import cv2

        self.size = size
        self.meta_ok = False
        self.maps: Optional[tuple[np.ndarray, np.ndarray]] = None
        self.det_size: Optional[tuple[int, int]] = None
        self.K_rect: Optional[np.ndarray] = None        # at display size
        self.K_rect_det: Optional[np.ndarray] = None    # at detector size
        self.K: Optional[np.ndarray] = None             # lens, display size
        self.D = np.zeros(0)
        self.model = "pinhole"
        self.raw = False
        if not meta or not meta.get("frame") or not meta.get("K_rect") \
                or not meta.get("calib"):
            return
        det = (int(meta["frame"][0]), int(meta["frame"][1]))
        cal = meta["calib"]
        try:
            self.K_rect = _scaled_K(np.reshape(meta["K_rect"], (3, 3)), det, size)
            self.K = _scaled_K(np.reshape(cal["K"], (3, 3)), det, size)
        except ValueError as e:
            log(f"[view] {e}; drawing without the camera model")
            self.K_rect = None
            return
        self.meta_ok = True
        self.det_size = det
        self.K_rect_det = np.reshape(meta["K_rect"], (3, 3)).astype(float)
        self.D = np.asarray(cal.get("D") or [], float)
        self.model = cal.get("model", "pinhole")
        corrected = meta.get("undistort") != "none" and bool(np.any(self.D != 0))
        if not corrected:
            return          # detector worked on the raw frame: nothing to do
        if raw:
            self.raw = True
            return
        init = (cv2.fisheye.initUndistortRectifyMap if self.model == "fisheye"
                else cv2.initUndistortRectifyMap)
        self.maps = init(self.K, self.D, np.eye(3), self.K_rect, size, cv2.CV_16SC2)

    def image(self, bgr: np.ndarray) -> np.ndarray:
        if self.maps is None:
            return bgr
        import cv2

        return cv2.remap(bgr, self.maps[0], self.maps[1], cv2.INTER_LINEAR)

    def corners(self, px, rec_size: tuple[int, int]) -> np.ndarray:
        """A record's corrected-image pixels -> displayed-frame pixels."""
        if not self.raw:
            return scale_points(px, self.det_size or rec_size, self.size)
        K = self.K_rect_det
        p = np.asarray(px, np.float64).reshape(-1, 2)
        n = np.column_stack([(p[:, 0] - K[0, 2]) / K[0, 0],
                             (p[:, 1] - K[1, 2]) / K[1, 1], np.ones(len(p))])
        return self.project(n, np.zeros(3), np.zeros(3))

    def project(self, obj: np.ndarray, rvec: np.ndarray, tvec: np.ndarray) -> np.ndarray:
        """Camera-frame (rvec, tvec) object points -> displayed-frame pixels."""
        import cv2

        obj = np.asarray(obj, np.float64).reshape(-1, 1, 3)
        rvec = np.asarray(rvec, np.float64).reshape(3, 1)
        tvec = np.asarray(tvec, np.float64).reshape(3, 1)
        if not self.raw:
            p, _ = cv2.projectPoints(obj, rvec, tvec, self.K_rect, None)
        elif self.model == "fisheye":
            p, _ = cv2.fisheye.projectPoints(obj, rvec, tvec, self.K, self.D)
        else:
            p, _ = cv2.projectPoints(obj, rvec, tvec, self.K, self.D)
        return p.reshape(-1, 2)


def _label(img: np.ndarray, lines: list[str], org: tuple[int, int],
           scale: float = 0.5, pad: int = 5, lh: int = 18) -> None:
    """Text block on a translucent dark box, kept inside the frame. As
    experiments/april-tag/apriltag_zmq_tracker.py draws it."""
    import cv2

    font = cv2.FONT_HERSHEY_SIMPLEX
    tw = max(cv2.getTextSize(t, font, scale, 1)[0][0] for t in lines)
    bw, bh = tw + 2 * pad, lh * len(lines) + pad
    H, W = img.shape[:2]
    x0 = min(max(org[0], 0), max(W - bw, 0))
    y0 = org[1] if org[1] + bh <= H else max(org[1] - bh - 16, 0)
    roi = img[y0:y0 + bh, x0:x0 + bw]
    roi[:] = (roi * 0.35).astype(np.uint8)
    for i, t in enumerate(lines):
        cv2.putText(img, t, (x0 + pad, y0 + lh * (i + 1) - 4), font, scale,
                    (255, 255, 255), 1, cv2.LINE_AA)


def annotate(img: np.ndarray, rec: dict, proj: Projector, axes: bool = True) -> np.ndarray:
    """Draw one `apriltag` record's tags onto `img` (BGR, display size), in place."""
    import cv2

    rec_size = (int(rec.get("w") or proj.size[0]), int(rec.get("h") or proj.size[1]))
    for tg in rec.get("tags") or []:
        warn = (tg.get("hamming") or 0) > 0 or (tg.get("ambiguity") or 0) > AMBIGUOUS
        col = WARN if warn else OK
        c = proj.corners(tg["corners_px"], rec_size)
        ci = np.int32(np.round(c))
        cv2.polylines(img, [ci], True, col, 2, cv2.LINE_AA)
        cv2.circle(img, tuple(int(v) for v in ci[0]), 5, col, -1, cv2.LINE_AA)

        if axes and proj.meta_ok and tg.get("quat") and tg.get("pos_m"):
            h = float(tg.get("size_m") or 0.1) / 2
            pts = np.float64([[0, 0, 0], [h, 0, 0], [0, h, 0], [0, 0, h]])
            p = np.int32(np.round(proj.project(pts, quat_to_rvec(tg["quat"]),
                                               np.asarray(tg["pos_m"], float))))
            # X red, Y green, Z blue. +z points into the tag (README conventions).
            for end, axis_col in ((3, (255, 0, 0)), (1, (0, 0, 255)), (2, (0, 255, 0))):
                cv2.line(img, tuple(int(v) for v in p[0]), tuple(int(v) for v in p[end]),
                         axis_col, 2, cv2.LINE_AA)

        lines = [f"id {tg['id']}   d {tg['dist_m']:.3f} m" if tg.get("dist_m") is not None
                 else f"id {tg['id']}"]
        if tg.get("pos_m"):
            x, y, z = tg["pos_m"]
            lines.append(f"x {x:+.3f}  y {y:+.3f}  z {z:+.3f}")
        if tg.get("euler_deg"):
            rx, ry, rz = tg["euler_deg"]
            lines.append(f"rx {rx:+.1f}  ry {ry:+.1f}  rz {rz:+.1f}")
        extra = []
        if tg.get("err_px") is not None:
            extra.append(f"err {tg['err_px']:.2f}px")
        if tg.get("ambiguity") is not None:
            extra.append(f"amb {tg['ambiguity']:.2f}")
        if tg.get("hamming"):
            extra.append(f"ham {tg['hamming']}")
        if extra:
            lines.append("  ".join(extra))
        _label(img, lines, (int(ci[:, 0].min()), int(ci[:, 1].max()) + 8))
    return img


def draw_hud(img: np.ndarray, lines: list[str], state: str) -> None:
    import cv2

    _label(img, lines, (0, 0))
    col = {"SYNCED": (0, 200, 0), "UNSYNCED": (0, 140, 255)}.get(state, (0, 0, 255))
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw, th), _ = cv2.getTextSize(state, font, 0.6, 2)
    x = img.shape[1] - tw - 10
    cv2.rectangle(img, (x - 6, 4), (x + tw + 6, th + 16), (0, 0, 0), -1)
    cv2.putText(img, state, (x, th + 10), font, 0.6, col, 2, cv2.LINE_AA)


# ---- runtime ---------------------------------------------------------------- #
class Mailbox:
    """The newest encoded frame. HTTP clients wait for the next version, so a
    slow client skips frames rather than queueing them."""

    def __init__(self) -> None:
        self.cv = threading.Condition()
        self.jpeg: Optional[bytes] = None
        self.version = 0

    def put(self, jpeg: bytes) -> None:
        with self.cv:
            self.jpeg = jpeg
            self.version += 1
            self.cv.notify_all()

    def wait_newer(self, version: int, timeout: float) -> tuple[int, Optional[bytes]]:
        with self.cv:
            if self.version == version:
                self.cv.wait(timeout)
            return self.version, self.jpeg


class Viewer:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.stop = threading.Event()
        self.cv = threading.Condition()
        self.frames = FrameBuffer(args.buffer)
        self.latest: Optional[tuple] = None         # (hdr, jpeg, t_recv)
        self.latest_n = 0
        self.pending: deque = deque()               # (rec, t_recv)
        self.meta: dict[str, dict] = {}
        self.out = Mailbox()
        self.t_last_rec = 0.0
        self.last_cam: Optional[str] = None
        self.last_tick: dict[str, int] = {}
        self.lost: dict[str, int] = defaultdict(int)
        self.rec_times: deque = deque(maxlen=60)
        self.view_times: deque = deque(maxlen=60)
        self.out_bytes = 0                          # last encoded frame
        self._proj: Optional[Projector] = None
        self._proj_key: Any = None

    # -- receivers ---------------------------------------------------------- #
    def _sub(self, address: str, topic: bytes, hwm: int):
        import zmq

        sock = zmq.Context.instance().socket(zmq.SUB)
        sock.setsockopt(zmq.RCVHWM, hwm)
        sock.setsockopt(zmq.RCVTIMEO, 250)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.SUBSCRIBE, topic)
        sock.connect(address)
        return sock

    def recv_frames(self) -> None:
        import zmq

        sock = self._sub(self.args.frames, b"", 2)
        log(f"[view] frames: SUB connect {self.args.frames}")
        try:
            while not self.stop.is_set():
                try:
                    parts = sock.recv_multipart()
                except zmq.Again:
                    continue
                got = wire.decode_frame(parts)
                if got is None:
                    continue
                hdr, jpeg = got
                item = (hdr, jpeg, time.monotonic())
                with self.cv:
                    if hdr.seq is not None:
                        self.frames.put(hdr.seq, item)
                    self.latest = item
                    self.latest_n += 1
                    self.cv.notify()
        finally:
            sock.close(0)

    def recv_tags(self) -> None:
        import zmq

        sock = self._sub(self.args.tags, wire.TOPIC_TAGS, 100)
        log(f"[view] tags: SUB connect {self.args.tags}")
        try:
            while not self.stop.is_set():
                try:
                    topic, body = sock.recv_multipart()
                except (zmq.Again, ValueError):
                    continue
                try:
                    rec = json.loads(body)
                except ValueError:
                    continue
                cam = rec.get("cam")
                if self.args.cam and cam != self.args.cam:
                    continue
                with self.cv:
                    if topic == wire.TOPIC_META:
                        self.meta[cam] = rec
                    elif topic == wire.TOPIC_TAGS:
                        self.pending.append((rec, time.monotonic()))
                        self.cv.notify()
        finally:
            sock.close(0)

    # -- rendering ---------------------------------------------------------- #
    def projector(self, meta: Optional[dict], size: tuple[int, int]) -> Projector:
        key = (size, json.dumps([meta.get(k) for k in ("frame", "K_rect", "calib",
                                                        "undistort")])
               if meta else None)
        if key != self._proj_key:
            self._proj = Projector(meta, size, self.args.raw)
            self._proj_key = key
        return self._proj  # type: ignore[return-value]

    def _note_record(self, rec: dict, now: float) -> None:
        cam = rec.get("cam")
        tick = rec.get("tick")
        if isinstance(tick, int):
            last = self.last_tick.get(cam)
            if last is not None and tick > last + 1:
                self.lost[cam] += tick - last - 1
            self.last_tick[cam] = tick
        self.rec_times.append(now)
        self.t_last_rec = now
        self.last_cam = cam

    def render(self, frame: tuple, rec: Optional[dict], state: str) -> None:
        import cv2

        hdr, jpeg, _ = frame
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return
        size = display_size((img.shape[1], img.shape[0]), self.args.scale)
        if size != (img.shape[1], img.shape[0]):
            img = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
        cam = rec.get("cam") if rec else self.last_cam
        proj = self.projector(self.meta.get(cam), size)
        img = proj.image(img)
        if rec is not None:
            annotate(img, rec, proj, axes=not self.args.no_axes)

        now = time.monotonic()
        self.view_times.append(now)
        view = _rate(self.view_times)
        lines = [f"{cam or hdr.cam or '?'}  {size[0]}x{size[1]}  seq {hdr.seq}"
                 f"  view {view:.1f}/s  det {_rate(self.rec_times):.1f}/s",
                 f"out {self.out_bytes / 1000:.0f} KB/frame"
                 f"  {self.out_bytes * 8 * view / 1e6:.1f} Mbit/s"]
        if rec is not None:
            ms = rec.get("ms") or {}
            lines.append(f"{rec.get('n', 0)} tag(s)  e2e {_ms(ms.get('e2e'))}"
                         f"  detect {_ms(ms.get('detect'))}  skipped {rec.get('skipped', 0)}"
                         f"  lost {self.lost[cam]}  clock {rec.get('clock')}")
        if not proj.meta_ok:
            lines.append("meta pending: frame shown uncorrected" if rec else
                         "no camera model")
        elif proj.raw:
            lines.append("raw view: overlay distorted to the lens")
        draw_hud(img, lines, state)

        ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.args.quality])
        if ok:
            self.out_bytes = len(enc)
            self.out.put(enc.tobytes())

    def render_loop(self) -> None:
        seen = 0
        period = 1 / self.args.max_fps if self.args.max_fps > 0 else 0.0
        t_render = -period
        while not self.stop.is_set():
            with self.cv:
                self.cv.wait(0.1)
                now = time.monotonic()
                draw: Optional[tuple] = None
                while self.pending:
                    rec, t_rec = self.pending[0]
                    frame = self.frames.get(rec.get("seq"))
                    if frame is not None:
                        draw = (frame, rec, "SYNCED")
                    elif (rec.get("seq") is None or now - t_rec > PENDING_S) \
                            and self.latest is not None:
                        draw = (self.latest, rec, "UNSYNCED")
                    elif now - t_rec <= PENDING_S:
                        break               # its frame may still be on the way
                    self.pending.popleft()
                    self._note_record(rec, now)
                if draw is None and self.latest is not None and self.latest_n != seen \
                        and now - self.t_last_rec > NO_DETECTOR_S:
                    draw = (self.latest, None, "NO DETECTOR")
                seen = self.latest_n if draw is not None else seen
            # Records are still consumed and counted above; only the drawing
            # is rate-limited.
            if draw is not None and now - t_render >= period:
                t_render = now
                self.render(*draw)

    # -- http --------------------------------------------------------------- #
    def serve(self) -> ThreadingHTTPServer:
        viewer = self
        page = (b"<!doctype html><title>april-detect viewer</title>"
                b"<body style='margin:0;background:#111'>"
                b"<img src='/stream' style='max-width:100%;max-height:100vh;"
                b"display:block;margin:auto'></body>")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                if self.path in ("/", "/index.html"):
                    self._send(200, "text/html", page)
                elif self.path.startswith("/snapshot"):
                    _, jpeg = viewer.out.wait_newer(-1, 0)
                    if jpeg is None:
                        self._send(503, "text/plain", b"no frame yet\n")
                    else:
                        self._send(200, "image/jpeg", jpeg)
                elif self.path.startswith("/stream"):
                    self._stream()
                else:
                    self._send(404, "text/plain", b"not found\n")

            def _send(self, code: int, ctype: str, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _stream(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type",
                                 "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SNDBUF)
                version = 0
                try:
                    while not viewer.stop.is_set():
                        v, jpeg = viewer.out.wait_newer(version, 1.0)
                        if v == version or jpeg is None:
                            continue
                        version = v
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                         b"Content-Length: " + str(len(jpeg)).encode()
                                         + b"\r\n\r\n" + jpeg + b"\r\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        srv = ThreadingHTTPServer((self.args.bind_host, self.args.http), Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
        log(f"[view] serving http://{self.args.bind_host}:{self.args.http}/")
        return srv

    def run(self) -> None:
        srv = self.serve()
        for fn in (self.recv_frames, self.recv_tags):
            threading.Thread(target=fn, name=fn.__name__, daemon=True).start()
        try:
            self.render_loop()
        finally:
            srv.shutdown()


def _rate(times: deque) -> float:
    if len(times) < 2 or times[-1] - times[0] <= 0:
        return 0.0
    if time.monotonic() - times[-1] > 2.0:
        return 0.0
    return (len(times) - 1) / (times[-1] - times[0])


def _ms(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.1f}ms"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--frames", required=True, help="camera PUB address (cam-sender)")
    p.add_argument("--tags", required=True, help="detector PUB address")
    p.add_argument("--cam", default="", help="only records with this `cam`")
    p.add_argument("--http", type=int, default=8080, help="HTTP port")
    p.add_argument("--bind-host", default="0.0.0.0")
    p.add_argument("--quality", type=int, default=60, help="output JPEG quality")
    p.add_argument("--scale", type=float, default=0.5,
                   help="output size relative to the camera frame (1 = full size)")
    p.add_argument("--max-fps", type=float, default=10,
                   help="output frame rate cap (0 = every frame)")
    p.add_argument("--buffer", type=int, default=60,
                   help="frames kept for pairing with records (~4 s at 15 fps)")
    p.add_argument("--raw", action="store_true",
                   help="show the uncorrected frame; distort the overlay instead")
    p.add_argument("--no-axes", action="store_true", help="outlines and labels only")
    args = p.parse_args(argv)

    v = Viewer(args)
    signal.signal(signal.SIGINT, lambda *_: v.stop.set())
    signal.signal(signal.SIGTERM, lambda *_: v.stop.set())
    v.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
