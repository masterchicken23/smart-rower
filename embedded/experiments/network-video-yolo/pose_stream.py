#!/usr/bin/env python3
"""
imageZMQ -> YOLO26 pose, with the inference schedule decoupled from the network.

Pipeline (three stages, no back-pressure between them):

  [recv thread]  imagezmq SUB/REP -> LatestSlot(1)   # only ever does recv; drains
                                                     # any socket backlog so the slot
                                                     # always holds the newest frame
  [infer thread] fixed-rate monotonic scheduler      # ticks at --fps regardless of
                                                     # arrival jitter; takes whatever
                                                     # is in the slot, emits JSONL
  [main thread]  optional cv2 GUI                    # r.plot() runs here, so drawing
                                                     # cost never enters the tick

Because the inference loop is deadline-driven and the slot is overwrite-on-write,
result latency is (decode + inference), not (network latency + queue depth). Late or
bursty frames are dropped, never queued; the tick period never stretches.

Landmarks go to STDOUT as JSONL (one object per tick). All logging goes to STDERR,
so `./pose_stream.py ... | jq .` is safe.

Two instances on one Jetson Orin Nano 8GB:
  - run two processes (one per stream); CUDA context per process is ~300MB
  - use --mem-fraction 0.4 to keep either process from starving the other
  - --torch-threads 2 --cv-threads 1 to avoid CPU oversubscription (6 A78 cores)
  - export TensorRT engines once and pass --model yolo26n-pose.engine to both:
        ./pose_stream.py --model yolo26n-pose.pt --imgsz 640 --half --export-engine
    engines cut latency ~2-3x and drop per-process host RAM a lot vs. torch
  - optionally enable CUDA MPS so the two processes overlap instead of time-slicing:
        sudo nvidia-cuda-mps-control -d
  - keep both at a combined tick budget the GPU can hold: 2x15fps at 640 on
    yolo26n-pose is comfortable; check the `late` counter in the stderr stats line

Requires: ultralytics, imagezmq, pyzmq, opencv-python, numpy
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import signal
import sys
import threading
import time
from typing import Any, Optional

COCO_KEYPOINTS = [
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
]

STOP = threading.Event()
_out_lock = threading.Lock()


def log(*a: Any) -> None:
    print(*a, file=sys.stderr, flush=True)


class Emitter(threading.Thread):
    """Writes JSONL to stdout off the inference thread.

    stdout was the last place the schedule still touched the outside world: a
    blocking write to a slow consumer -- a terminal repainting, a jq that
    stalls, a pipe whose reader pauses -- stalls the tick directly. Measured at
    up to 400ms for one write to a stalled pipe, which is what an out_ms spike
    with a normal infer_ms means.

    On overflow the OLDEST record is dropped, keeping the stream current. Gaps
    are detectable downstream because "tick" is a gapless counter.
    """

    def __init__(self, maxsize: int) -> None:
        super().__init__(name="emit", daemon=True)
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.dropped = 0
        self._stop = object()

    def put(self, obj: dict) -> None:
        line = json.dumps(obj, separators=(",", ":"))
        while True:
            try:
                self.q.put_nowait(line)
                return
            except queue.Full:
                try:
                    self.q.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    pass

    def run(self) -> None:
        while True:
            item = self.q.get()
            if item is self._stop:
                return
            try:
                sys.stdout.write(item + "\n")
                sys.stdout.flush()
            except Exception:  # noqa: BLE001
                return  # stdout closed (consumer went away): stop writing

    def close(self, timeout: float = 2.0) -> None:
        try:
            self.q.put(self._stop, timeout=0.5)
        except queue.Full:
            pass
        self.join(timeout)


class Publisher(threading.Thread):
    """Republish each record on a PUB socket, off the inference thread.

    Carries the full internal record, not the mobile-app shape: downstream
    consumers can narrow it, but cannot reconstruct what was dropped. Meta is
    resent periodically because a SUB that joins later would otherwise never
    learn the keypoint names, and with --keypoints body the indices are not
    COCO's.
    """

    TOPIC_POSE = b"pose"
    TOPIC_META = b"meta"

    def __init__(self, address: str, hwm: int, meta_period: float,
                 bind: bool = True) -> None:
        super().__init__(name="pub", daemon=True)
        self.address, self.hwm, self.bind = address, hwm, bind
        self.meta_period = meta_period
        self.q: queue.Queue = queue.Queue(maxsize=64)
        self.meta: Optional[dict] = None
        self.sent = 0
        self.dropped = 0
        self.ready = threading.Event()

    def put(self, topic: bytes, obj: dict) -> None:
        if topic == self.TOPIC_META:
            self.meta = obj
        try:
            self.q.put_nowait((topic, obj))
        except queue.Full:
            try:
                self.q.get_nowait()
                self.dropped += 1
                self.q.put_nowait((topic, obj))
            except (queue.Empty, queue.Full):
                self.dropped += 1

    def run(self) -> None:
        import zmq

        ctx = zmq.Context()
        sock = ctx.socket(zmq.PUB)
        sock.setsockopt(zmq.SNDHWM, self.hwm)   # drop rather than buffer
        sock.setsockopt(zmq.LINGER, 0)
        try:
            if self.bind:
                sock.bind(self.address)
            else:
                sock.connect(self.address)
        except Exception as e:  # noqa: BLE001
            log(f"[pub] cannot {'bind' if self.bind else 'connect'} "
                f"{self.address}: {e}")
            self.ready.set()
            return
        log(f"[pub] PUB {'bind' if self.bind else 'connect'} {self.address}")
        self.ready.set()
        next_meta = 0.0
        while not STOP.is_set():
            now = time.monotonic()
            if self.meta is not None and now >= next_meta:
                next_meta = now + self.meta_period
                try:
                    sock.send_multipart(
                        [self.TOPIC_META,
                         json.dumps(self.meta, separators=(",", ":")).encode()])
                except Exception:  # noqa: BLE001
                    pass
            try:
                topic, obj = self.q.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                sock.send_multipart(
                    [topic, json.dumps(obj, separators=(",", ":")).encode()])
                self.sent += 1
            except Exception as e:  # noqa: BLE001
                log(f"[pub] send failed: {e!r}")
        sock.close()
        ctx.term()


_EMITTER: Optional[Emitter] = None
_PUBLISHER: Optional[Publisher] = None
_NO_STDOUT = False


def emit(obj: dict) -> None:
    p = _PUBLISHER
    if p is not None:
        p.put(Publisher.TOPIC_META if obj.get("type") == "meta"
              else Publisher.TOPIC_POSE, obj)
    e = _EMITTER
    if e is not None:
        e.put(obj)          # never blocks the caller
        return
    if _NO_STDOUT:
        return
    line = json.dumps(obj, separators=(",", ":"))
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


# COCO indices. Face points (0-4) carry no information for a side-view rower.
KP_I = {n: i for i, n in enumerate(COCO_KEYPOINTS)}
BODY_IDX = list(range(5, 17))
LEFT_IDX = [KP_I[n] for n in ("left_shoulder", "left_elbow", "left_wrist",
                              "left_hip", "left_knee", "left_ankle")]
RIGHT_IDX = [KP_I[n] for n in ("right_shoulder", "right_elbow", "right_wrist",
                               "right_hip", "right_knee", "right_ankle")]

# Defaults applied when the corresponding flag is left unset. --rowing tunes for
# a single side-on subject; anything passed explicitly still wins.
BASE_DEFAULTS = {"conf": 0.25, "max_det": 10, "kp_conf": 0.0,
                 "keypoints": "all", "side": "off", "smooth_hz": 0.0}
ROWING_DEFAULTS = {"conf": 0.15, "max_det": 1, "kp_conf": 0.3,
                   "keypoints": "body", "side": "auto", "smooth_hz": 3.0}


class SideSelector:
    """Which side of the body faces the camera.

    In a side view the far limbs are occluded, and the model still emits them --
    often mirrored onto the near limb or hallucinated. Picking the near side per
    frame flaps, because a single bad frame flips it, so the confidence
    difference is low-passed and switching needs a margin.
    """

    def __init__(self, mode: str, alpha: float = 0.1, hyst: float = 0.05) -> None:
        self.mode = mode
        self.alpha, self.hyst = alpha, hyst
        self.ema: Optional[float] = None
        # "off" means emit no side at all, so it stays None
        self.side: Optional[str] = mode if mode in ("left", "right") else None

    def update(self, conf) -> Optional[str]:
        if self.mode in ("off", "left", "right"):
            return self.side
        import numpy as np

        d = float(np.mean(conf[LEFT_IDX]) - np.mean(conf[RIGHT_IDX]))
        self.ema = d if self.ema is None else self.ema + self.alpha * (d - self.ema)
        if self.ema > self.hyst:
            self.side = "left"
        elif self.ema < -self.hyst:
            self.side = "right"
        elif self.side is None:
            self.side = "left" if self.ema >= 0 else "right"
        return self.side


class Smoother:
    """One-pole low-pass on keypoint positions.

    Valid only because the tick is uniform: a fixed coefficient assumes a fixed
    sample interval. alpha = 1 - exp(-2*pi*fc/fps), group delay ~= 1/(2*pi*fc)
    seconds -- 6 Hz costs about 27ms, which is well inside a stroke.

    Identity-blind, so it is only safe with a single detection; mixing two
    people through one filter would interpolate between them.
    """

    def __init__(self, fc: float, fps: float) -> None:
        self.alpha = 1.0 - math.exp(-2.0 * math.pi * fc / fps) if fc > 0 else 1.0
        self.prev = None
        self.have = None

    @property
    def enabled(self) -> bool:
        return self.alpha < 1.0

    def describe(self, fc: float) -> str:
        lag = 1000.0 / (2.0 * math.pi * fc) if fc > 0 else 0.0
        note = "  (weak: cutoff is close to the tick rate)" if self.alpha > 0.85 else ""
        return f"fc={fc:g}Hz alpha={self.alpha:.2f} lag~{lag:.0f}ms{note}" 

    def reset(self) -> None:
        self.prev = self.have = None

    def apply(self, xy, valid):
        if not self.enabled:
            return xy
        import numpy as np

        if self.prev is None:
            self.prev = xy.copy()
            self.have = valid.copy()
            return xy
        m = (self.have & valid)[:, None]
        out = np.where(m, self.prev + self.alpha * (xy - self.prev), xy)
        self.prev = np.where(valid[:, None], out, self.prev)
        self.have = self.have | valid
        return out


def _angle_at(a, b, c) -> Optional[float]:
    """Interior angle at b in degrees; 180 is a straight joint."""
    import numpy as np

    if a is None or b is None or c is None:
        return None
    v1, v2 = a - b, c - b
    n1, n2 = float(np.linalg.norm(v1)), float(np.linalg.norm(v2))
    if n1 < 1e-6 or n2 < 1e-6:
        return None
    cos = float(np.dot(v1, v2) / (n1 * n2))
    return round(math.degrees(math.acos(max(-1.0, min(1.0, cos)))), 1)


def rowing_measures(xy, valid, side: str) -> dict:
    """Per-frame geometry for a side-on rower, in PIXELS.

    Computed before any normalisation on purpose: --normalize scales x and y by
    different factors, which would skew every angle derived from it.
    """
    def pt(name):
        i = KP_I[f"{side}_{name}"]
        return xy[i] if valid[i] else None

    sh, el, wr = pt("shoulder"), pt("elbow"), pt("wrist")
    hip, kn, an = pt("hip"), pt("knee"), pt("ankle")

    trunk = None
    if sh is not None and hip is not None:
        v = sh - hip  # image coords: +y is down, so -v[1] points up the frame
        trunk = round(math.degrees(math.atan2(float(v[0]), float(-v[1]))), 1)

    return {
        # hip is the slide-position signal; reference it to your own catch/finish
        "hip": [round(float(hip[0]), 1), round(float(hip[1]), 1)]
              if hip is not None else None,
        "shoulder": [round(float(sh[0]), 1), round(float(sh[1]), 1)]
                    if sh is not None else None,
        "wrist": [round(float(wr[0]), 1), round(float(wr[1]), 1)]
                 if wr is not None else None,
        # signed from vertical; sign follows which way the rower faces in frame
        "trunk_deg": trunk,
        "knee_deg": _angle_at(hip, kn, an),
        "elbow_deg": _angle_at(sh, el, wr),
    }


# --------------------------------------------------------------------------- #
# single-slot latest-value buffer: writer never blocks, reader never backlogs
# --------------------------------------------------------------------------- #
class LatestSlot:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._item: Optional[tuple] = None
        self._seq = 0
        self._dropped = 0

    def put(self, name: str, payload: Any, t_recv: float) -> None:
        with self._lock:
            if self._item is not None:
                self._dropped += 1  # previous frame was never consumed
            self._seq += 1
            self._item = (self._seq, name, payload, t_recv)

    def take(self) -> Optional[tuple]:
        """Consume the current frame, or None if nothing new since last take."""
        with self._lock:
            item, self._item = self._item, None
            return item

    def drop_count(self) -> int:
        with self._lock:
            d, self._dropped = self._dropped, 0
            return d


# --------------------------------------------------------------------------- #
# receiver
# --------------------------------------------------------------------------- #
class Receiver(threading.Thread):
    """Does nothing but recv. Any time spent here is time not spent inferring."""

    def __init__(self, args, slot: LatestSlot) -> None:
        super().__init__(name="recv", daemon=True)
        self.args = args
        self.slot = slot
        self.hub = None
        self.n_recv = 0
        self.n_drained = 0

    def _connect(self):
        import imagezmq
        import zmq

        req_rep = self.args.mode == "reqrep"
        log(f"[recv] {'REQ/REP bind' if req_rep else 'PUB/SUB connect'} "
            f"-> {self.args.connect}")
        hub = imagezmq.ImageHub(open_port=self.args.connect, REQ_REP=req_rep)
        sock = getattr(hub, "zmq_socket", None)
        if sock is not None:
            # bounded recv so this thread can notice STOP instead of parking
            # forever on a silent sender, and LINGER 0 so teardown can't block
            sock.setsockopt(zmq.RCVTIMEO, int(self.args.recv_timeout_ms))
            sock.setsockopt(zmq.LINGER, 0)
        return hub

    def _drain(self, name, payload):
        """PUB/SUB only: pull everything already queued so we keep the newest."""
        import zmq

        sock = getattr(self.hub, "zmq_socket", None)
        if sock is None:
            return name, payload
        recv = sock.recv_image if self.args.raw else sock.recv_jpg
        for _ in range(64):
            try:
                name, payload = recv(flags=zmq.NOBLOCK, copy=False)
                self.n_drained += 1
            except zmq.Again:
                break
            except Exception:
                break
        return name, payload

    def run(self) -> None:
        import zmq

        backoff = 0.5
        while not STOP.is_set():
            try:
                if self.hub is None:
                    self.hub = self._connect()
                    backoff = 0.5

                if self.args.raw:
                    name, payload = self.hub.recv_image()
                else:
                    name, payload = self.hub.recv_jpg()

                if self.args.mode == "reqrep":
                    # reply immediately: the sender must never wait on inference
                    self.hub.send_reply(b"OK")
                else:
                    name, payload = self._drain(name, payload)

                self.n_recv += 1
                self.slot.put(name, payload, time.monotonic())

            except zmq.Again:
                continue  # recv timeout: no frames right now, just re-check STOP
            except zmq.ZMQError as e:
                if STOP.is_set():
                    break
                log(f"[recv] zmq error: {e!r}; reconnecting in {backoff:.1f}s")
                self._close()
                STOP.wait(backoff)
                backoff = min(backoff * 2, 5.0)
            except Exception as e:  # noqa: BLE001
                if STOP.is_set():
                    break
                log(f"[recv] error: {e!r}; reconnecting in {backoff:.1f}s")
                self._close()
                STOP.wait(backoff)
                backoff = min(backoff * 2, 5.0)
        self._close()

    def _close(self) -> None:
        try:
            if self.hub is not None:
                self.hub.close()
        except Exception:  # noqa: BLE001
            pass
        self.hub = None


# --------------------------------------------------------------------------- #
# inference
# --------------------------------------------------------------------------- #
class Inferencer(threading.Thread):
    def __init__(self, args, slot: LatestSlot, display: Optional[LatestSlot]) -> None:
        super().__init__(name="infer", daemon=True)
        self.args = args
        self.slot = slot
        self.display = display
        self.model = None
        self.ready = threading.Event()
        self.side_sel = SideSelector(args.side)
        self.smoother = Smoother(args.smooth_hz, args.fps)
        self.emit_idx = (BODY_IDX if args.keypoints == "body"
                         else list(range(len(COCO_KEYPOINTS))))
        # stats for the periodic stderr line
        self.n_ticks = 0
        self.n_infer = 0
        self.n_stale = 0
        self.n_late = 0
        self.infer_ms_sum = 0.0
        self.jitter_ms_max = 0.0

    # -- model ------------------------------------------------------------- #
    def _load(self):
        import cv2  # noqa: F401  (imported here so --help stays fast)
        import numpy as np
        import torch

        YOLO = import_yolo()
        a = self.args
        cuda = a.device.startswith("cuda") or a.device.isdigit()
        if cuda and a.mem_fraction and torch.cuda.is_available():
            # hard cap so two processes can coexist in the 8GB shared with the CPU
            idx = int(a.device.split(":")[-1]) if ":" in a.device else \
                (int(a.device) if a.device.isdigit() else 0)
            torch.cuda.set_per_process_memory_fraction(a.mem_fraction, idx)
        torch.set_num_threads(a.torch_threads)
        if cuda:
            torch.backends.cudnn.benchmark = True  # fixed imgsz -> stable kernels

        log(f"[infer] loading {a.model} on {a.device}")
        model = YOLO(a.model, task="pose")

        # engines have precision baked in; FP16 only applies to torch weights
        self.use_half = bool(a.half) and cuda and not str(a.model).endswith(".engine")
        self.prec = precision_kwargs(self.use_half, str(a.model))

        dummy = np.zeros((a.imgsz, a.imgsz, 3), dtype=np.uint8)
        for _ in range(a.warmup):
            model.predict(dummy, imgsz=a.imgsz, device=a.device, conf=a.conf,
                          iou=a.iou, max_det=a.max_det, verbose=False, **self.prec)
        log(f"[infer] warm ({a.warmup} passes), precision="
            f"{self.prec or 'from engine'}")
        if a.rowing:
            log(f"[infer] rowing profile: max_det={a.max_det} conf={a.conf} "
                f"kp_conf={a.kp_conf} side={a.side} keypoints={a.keypoints}")
        if self.smoother.enabled:
            log(f"[infer] smoothing {self.smoother.describe(a.smooth_hz)}")
        return model

    # -- scheduler --------------------------------------------------------- #
    def run(self) -> None:
        import cv2
        import numpy as np

        a = self.args
        try:
            self.model = self._load()
        except Exception as e:  # noqa: BLE001
            log(f"[infer] fatal: model load failed: {e!r}")
            STOP.set()
            self.ready.set()
            return
        self.ready.set()

        period = 1.0 / a.fps
        deadline = time.monotonic() + period
        last_name = None

        while not STOP.is_set():
            now = time.monotonic()
            if now < deadline:
                if STOP.wait(deadline - now):
                    break
                t0 = time.monotonic()
            else:
                t0 = now  # already past the deadline: the previous tick overran

            t_wall = time.time()   # one stamp per tick, taken at tick start, so the
                                   # emitted timestamps sit on an even grid regardless
                                   # of how long inference takes
            lateness = t0 - deadline
            jitter_ms = lateness * 1000.0
            self.jitter_ms_max = max(self.jitter_ms_max, abs(jitter_ms))
            if lateness >= period:
                # overran by one or more whole periods: drop those grid points
                # rather than letting the schedule drift late forever
                skipped = int(lateness // period)
                self.n_late += skipped
                deadline += skipped * period
            deadline += period
            self.n_ticks += 1
            tick = self.n_ticks

            item = self.slot.take()
            if item is None:
                self.n_stale += 1
                if not a.quiet_stale:
                    emit({"type": "pose", "tick": tick, "t": t_wall,
                          "src": last_name, "stale": True, "n": 0, "people": [],
                          "jitter_ms": round(jitter_ms, 2)})
                continue

            seq, name, payload, t_recv = item
            last_name = name
            age_ms = (t0 - t_recv) * 1000.0

            if a.max_age_ms and age_ms > a.max_age_ms:
                # frame older than the freshness budget: don't burn GPU on it
                self.n_stale += 1
                if not a.quiet_stale:
                    emit({"type": "pose", "tick": tick, "t": t_wall, "seq": seq,
                          "src": name, "stale": True, "age_ms": round(age_ms, 2),
                          "n": 0, "people": [], "jitter_ms": round(jitter_ms, 2)})
                continue

            try:
                t_dec = time.monotonic()
                if a.raw:
                    frame = payload
                else:
                    buf = payload if isinstance(payload, np.ndarray) else \
                        np.frombuffer(payload, dtype=np.uint8)
                    frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
                if frame is None:
                    log("[infer] decode failed, skipping frame")
                    continue

                dec_ms = (time.monotonic() - t_dec) * 1000.0
                t_inf = time.monotonic()
                res = self.model.predict(
                    frame, imgsz=a.imgsz, device=a.device, conf=a.conf, iou=a.iou,
                    max_det=a.max_det, verbose=False, **self.prec,
                )[0].cpu()
                infer_ms = (time.monotonic() - t_inf) * 1000.0
            except Exception as e:  # noqa: BLE001
                log(f"[infer] inference error: {e!r}")
                continue

            self.n_infer += 1
            self.infer_ms_sum += infer_ms

            h, w = frame.shape[:2]
            emit(self._record(res, tick, t_wall, t0, seq, name, w, h,
                              age_ms, dec_ms, infer_ms, jitter_ms))

            if self.display is not None:
                self.display.put(name, res, time.monotonic())

    # -- display ------------------------------------------------------------ #
    def _overlay(self, data, i, xy, valid) -> None:
        """Write smoothed positions and gating back for res.plot().

        conf is zeroed rather than the point removed, because that is what makes
        the annotator skip a joint; the JSON keeps the true confidence, which was
        copied out before this runs.
        """
        a = self.args
        try:
            data[i, :, 0] = xy[:, 0]
            data[i, :, 1] = xy[:, 1]
            if a.kp_conf > 0:
                for k in range(data.shape[1]):
                    if not valid[k]:
                        data[i, k, 2] = 0.0
            if a.keypoints == "body":
                for k in range(min(5, data.shape[1])):  # face points
                    data[i, k, 2] = 0.0
        except Exception as e:  # noqa: BLE001
            if not getattr(self, "_overlay_warned", False):
                self._overlay_warned = True
                log(f"[gui] cannot apply filtering to the overlay ({e!r}); "
                    f"showing raw model output")

    # -- json -------------------------------------------------------------- #
    def _record(self, res, tick, t_wall, t0, seq, name, w, h,
                age_ms, dec_ms, infer_ms, jitter_ms) -> dict:
        import numpy as np

        a = self.args
        people = []
        side = None
        rowing = None
        kps = getattr(res, "keypoints", None)
        boxes = getattr(res, "boxes", None)
        if kps is not None and kps.data is not None and len(kps.data):
            kdata = kps.data.numpy()  # (N, K, 3) -> x, y, conf
            bxyxy = boxes.xyxy.numpy() if boxes is not None else None
            bconf = boxes.conf.numpy() if boxes is not None else None
            bid = boxes.id.numpy() if (boxes is not None and boxes.id is not None) else None
            sx, sy = (1.0 / w, 1.0 / h) if a.normalize else (1.0, 1.0)
            nd = 4 if a.normalize else 1
            for i in range(kdata.shape[0]):
                xy = kdata[i, :, :2].astype(np.float64)
                conf = kdata[i, :, 2].astype(np.float64)
                valid = conf >= a.kp_conf if a.kp_conf > 0 else np.ones_like(conf, bool)

                # person 0 is the highest-confidence detection: the subject
                if i == 0:
                    if self.smoother.enabled:
                        xy = self.smoother.apply(xy, valid)
                    side = self.side_sel.update(conf)
                    if side and a.rowing:
                        rowing = rowing_measures(xy, valid, side)

                # Feed the filtered values back into the Results object so the
                # GUI draws what the pipeline actually produced. Without this the
                # overlay is the raw model output and looks unsmoothed no matter
                # how hard --smooth-hz is driven, while the JSON is filtered --
                # two different answers on screen and on stdout.
                if self.display is not None and not a.gui_raw:
                    self._overlay(kps.data, i, xy, valid)

                person: dict = {
                    "kp": [
                        ([round(float(xy[k, 0]) * sx, nd),
                          round(float(xy[k, 1]) * sy, nd),
                          round(float(conf[k]), 3)] if valid[k]
                         else [None, None, round(float(conf[k]), 3)])
                        for k in self.emit_idx
                    ]
                }
                if bxyxy is not None and i < len(bxyxy):
                    x1, y1, x2, y2 = bxyxy[i]
                    person["box"] = [round(float(x1) * sx, nd), round(float(y1) * sy, nd),
                                     round(float(x2) * sx, nd), round(float(y2) * sy, nd)]
                if bconf is not None and i < len(bconf):
                    person["score"] = round(float(bconf[i]), 3)
                if bid is not None and i < len(bid):
                    person["id"] = int(bid[i])
                people.append(person)
        else:
            self.smoother.reset()  # subject lost: do not drag stale positions in

        rec = {
            "type": "pose",
            "tick": tick,
            "t": t_wall,          # tick start (even grid), not emit time
            "seq": seq,
            "src": name,
            "w": w, "h": h,
            "stale": False,
            "age_ms": round(age_ms, 2),      # frame arrival -> tick start
            "dec_ms": round(dec_ms, 2),        # JPEG decode
            "infer_ms": round(infer_ms, 2),
            "jitter_ms": round(jitter_ms, 2),  # tick start - scheduled deadline
            # tick start -> record built = dec_ms + infer_ms + record overhead.
            # It does NOT include the stdout write, which happens after this and
            # is handed to the emitter thread anyway.
            "out_ms": round((time.monotonic() - t0) * 1000.0, 2),
            "n": len(people),
            "people": people,
        }
        if side is not None:
            rec["side"] = side          # which side faces the camera
        if rowing is not None:
            rec["rowing"] = rowing      # pixel-space geometry for person 0
        return rec


# --------------------------------------------------------------------------- #
def reporter(args, recv: Receiver, inf: Inferencer, slot: LatestSlot) -> None:
    if args.stats_interval <= 0:
        return
    prev = (0, 0, 0, 0, 0)
    while not STOP.wait(args.stats_interval):
        r, i, t, s, l = recv.n_recv, inf.n_infer, inf.n_ticks, inf.n_stale, inf.n_late
        pr, pi, pt, ps, pl = prev
        dt = args.stats_interval
        avg = inf.infer_ms_sum / max(i - pi, 1)
        outq = f"  out_drop {_EMITTER.dropped:4d}" if _EMITTER is not None else ""
        if _PUBLISHER is not None:
            outq += f"  pub {_PUBLISHER.sent:6d}/{_PUBLISHER.dropped:d}"
        log(f"[stats] rx {(r-pr)/dt:5.1f}/s  infer {(i-pi)/dt:5.1f}/s  "
            f"tick {(t-pt)/dt:5.1f}/s  stale {s-ps:3d}  late {l-pl:3d}  "
            f"drop {slot.drop_count():4d}  drained {recv.n_drained:5d}  "
            f"infer_avg {avg:5.1f}ms  jitter_max {inf.jitter_ms_max:5.2f}ms{outq}")
        inf.infer_ms_sum = 0.0
        inf.jitter_ms_max = 0.0
        prev = (r, i, t, s, l)


def gui_loop(args, display: LatestSlot) -> None:
    import cv2

    win = f"yolo26-pose [{args.connect}]"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    period = 1.0 / args.display_fps
    last = None
    while not STOP.is_set():
        item = display.take()
        if item is not None:
            _, _, res, _ = item
            try:
                last = res.plot()  # drawing cost lives here, not in the tick
            except Exception as e:  # noqa: BLE001
                log(f"[gui] plot error: {e!r}")
        if last is not None:
            cv2.imshow(win, last)
        if cv2.waitKey(max(1, int(period * 1000))) & 0xFF in (ord("q"), 27):
            log("[gui] quit")
            STOP.set()
            break
    cv2.destroyAllWindows()


def export_engine(args) -> int:
    if args.model.endswith(".engine"):
        raise SystemExit(
            f"--export-engine needs the source checkpoint, not an engine.\n"
            f"  --model {args.model[:-len('.engine')]}.pt --export-engine")
    if not args.allow_autoinstall:
        # Ultralytics' check_requirements pip-installs anything it thinks is
        # missing. On Jetson that means the desktop tensorrt/onnxruntime wheels,
        # which then shadow JetPack's and fail at runtime -- exactly the loop
        # this script exists to avoid. Fail loudly instead.
        os.environ["YOLO_AUTOINSTALL"] = "false"
        log("[export] auto-install disabled (--allow-autoinstall to permit); "
            "missing deps will be reported, not pip-installed")
    YOLO = import_yolo()
    log(f"[export] {args.model} -> TensorRT (imgsz={args.imgsz}, half={args.half})")
    log("[export] this takes several minutes on an Orin Nano and the fan will "
        "spin up; do not interrupt it")
    try:
        path = YOLO(args.model, task="pose").export(
            format="engine", imgsz=args.imgsz, device=args.device,
            workspace=args.trt_workspace, batch=1,
            **precision_kwargs(args.half),
        )
    except ImportError as e:
        raise SystemExit(
            f"export failed on a missing dependency: {e}\n\n"
            "TensorRT export needs onnx and onnxruntime-gpu. Ultralytics tries to\n"
            "pip-install them automatically, which fails on Jetson because PyPI has\n"
            "no aarch64 onnxruntime-gpu build. Install the Tegra wheel -- see\n"
            "INSTALL.md -- then retry.")
    except TypeError as e:
        if "returned nullptr" not in str(e):
            raise
        raise SystemExit(
            f"TensorRT could not create a builder: {e}\n\n"
            "The bindings imported but failed to initialise against the driver.\n"
            "On a Jetson inside a venv this is almost always a pip-installed\n"
            "tensorrt shadowing the one JetPack provides: Ultralytics runs\n"
            "check_requirements('tensorrt') during export, and if the system\n"
            "package is not visible it installs the PyPI wheel, which is built\n"
            "for desktop CUDA and cannot open a Tegra device.\n\n"
            "Diagnose:\n"
            f"  python3 {os.path.basename(sys.argv[0]) or 'pose_stream.py'} --check-env\n\n"
            "If tensorrt resolves inside .venv/lib/.../site-packages, drop it and\n"
            "let the system one through:\n"
            "  pip uninstall -y tensorrt tensorrt-cu12 tensorrt_lean tensorrt_dispatch\n"
            "  # the venv must have been created with --system-site-packages:\n"
            "  python3 -m venv --system-site-packages .venv\n\n"
            "If torch.cuda.is_available() is False, that is the real problem --\n"
            "the Tegra torch wheel is not installed (see INSTALL.md); TensorRT\n"
            "cannot build without a working CUDA context.")
    log(f"[export] wrote {path}")
    log(f"[export] now run:  --model {path}")
    return 0


def check_env(args) -> int:
    """Report what this interpreter actually resolves, for Jetson triage."""
    import platform

    venv = sys.prefix != sys.base_prefix
    print(f"python        {platform.python_version()}  {sys.executable}")
    print(f"venv          {'yes' if venv else 'no'}"
          f"{'  (system-site-packages: ' + ('yes' if _sees_system_packages() else 'NO') + ')' if venv else ''}")
    print(f"machine       {platform.machine()}")

    try:
        import torch

        cuda = torch.cuda.is_available()
        print(f"torch         {torch.__version__}  cuda_available={cuda}")
        if cuda:
            print(f"  device      {torch.cuda.get_device_name(0)}")
            print(f"  cuda build  {torch.version.cuda}")
        else:
            print("  !! no CUDA. On Jetson this means the Tegra torch wheel is not")
            print("     installed -- PyPI torch has no Tegra support. See INSTALL.md.")
    except Exception as e:  # noqa: BLE001
        print(f"torch         MISSING/BROKEN: {e}")

    try:
        import tensorrt as trt

        where = getattr(trt, "__file__", "?")
        print(f"tensorrt      {getattr(trt, '__version__', '?')}  {where}")
        if "site-packages" in (where or ""):
            import glob

            # name only tensorrt paths -- never anything that could expand to
            # site-packages itself
            pkg = (os.path.dirname(where)
                   if os.path.basename(where) == "__init__.py" else where)
            parent, leaf = os.path.dirname(pkg), os.path.basename(pkg)
            print("  !! resolved from pip site-packages, not JetPack's")
            print("     dist-packages. The PyPI wheel targets desktop CUDA and")
            print("     cannot open a Tegra device -- this is the usual cause of")
            print("     'pybind11::init(): factory function returned nullptr'.")
            print("     pip uninstall -y tensorrt tensorrt-cu12 tensorrt_lean "
                  "tensorrt_dispatch")
            print("     pip list | grep -iE 'tensorrt|nvidia'   # find any that remain")
            if parent and leaf.startswith("tensorrt"):
                print(f"     rm -rf {parent}/tensorrt {parent}/tensorrt.py "
                      f"{parent}/tensorrt-*.dist-info {parent}/tensorrt_libs")
                print("       (pip reports success but can leave files behind,")
                print("        which still shadow the system copy)")
            sysdirs = sorted(glob.glob("/usr/lib/python3*/dist-packages/tensorrt"))
            if sysdirs:
                print(f"     JetPack's copy is present at {sysdirs[0]} and takes")
                print("     over once the pip one is gone.")
            else:
                print("     No JetPack tensorrt found either -- install it:")
                print("       sudo apt install -y python3-libnvinfer")
        else:
            try:
                trt.Builder(trt.Logger(trt.Logger.ERROR))
                print("  builder     OK")
            except Exception as e:  # noqa: BLE001
                print(f"  !! builder  FAILED: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"tensorrt      MISSING/BROKEN: {e}")
        print("  JetPack ships it as an apt package with no pip wheel; a venv")
        print("  must be created with --system-site-packages to see it.")

    for mod in ("ultralytics", "cv2", "numpy", "imagezmq", "zmq"):
        try:
            m = __import__(mod)
            print(f"{mod:<13} {getattr(m, '__version__', 'ok'):<12} "
                  f"{getattr(m, '__file__', '')}")
        except Exception as e:  # noqa: BLE001
            print(f"{mod:<13} MISSING: {e}")
    return 0


def _sees_system_packages() -> bool:
    return any("dist-packages" in p for p in sys.path)


def precision_kwargs(half: bool, model: str = "") -> dict:
    """FP16 selector, spelled for whichever ultralytics is installed.

    8.4 renamed half=True to quantize=16 and warns on the old name; older
    releases only know half=. Exported artifacts (.engine) carry their precision
    internally, so pass nothing and let the runtime decide.
    """
    if model.endswith(".engine"):
        return {}
    try:
        from ultralytics.cfg import DEFAULT_CFG_DICT

        if "quantize" in DEFAULT_CFG_DICT:
            return {"quantize": 16 if half else 32}
    except Exception:  # noqa: BLE001
        pass
    return {"half": half}


def import_yolo():
    try:
        from ultralytics import YOLO

        return YOLO
    except ImportError as e:
        raise SystemExit(
            f"cannot import ultralytics: {e}\n\n"
            "  pip install ultralytics\n\n"
            "On a Jetson, install it BEFORE overwriting torch/torchvision with the\n"
            "Tegra wheels -- see INSTALL.md; PyPI's torch has no Tegra CUDA support.")


def check_model(args) -> None:
    """A missing .engine is the common first-run stumble; say how to build it.

    Engines are compiled for one device, one TensorRT/JetPack version and one
    --imgsz, so they are never downloaded with the weights -- you build one on
    the Jetson that will run it.
    """
    if not args.model.endswith(".engine") or os.path.exists(args.model):
        return
    pt = args.model[: -len(".engine")] + ".pt"
    me = os.path.basename(sys.argv[0]) or "pose_stream.py"
    half = " --half" if args.half else " --no-half"
    raise SystemExit(
        f"TensorRT engine not found: {args.model}\n\n"
        "Engines are built on the device -- they are specific to this Jetson, its\n"
        "TensorRT/JetPack version and --imgsz, so nothing downloads one for you.\n"
        "Build it once (several minutes):\n\n"
        f"  python3 {me} --model {pt} --imgsz {args.imgsz}{half} --export-engine\n\n"
        f"That downloads {os.path.basename(pt)} if needed and writes the .engine\n"
        "beside it. Re-run with the path it prints -- use an absolute path if you\n"
        "start the receiver from a different directory.\n\n"
        "Or skip TensorRT and run the checkpoint directly (slower, more host RAM,\n"
        "but no build step):\n\n"
        f"  python3 {me} --model {pt}"
    )


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="imageZMQ JPEG stream -> YOLO26 pose, fixed-rate JSONL on stdout.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src = p.add_argument_group("source")
    src.add_argument("--connect", default="tcp://127.0.0.1:5555",
                     help="imageZMQ publisher address to connect to")
    src.add_argument("--mode", choices=("pubsub", "reqrep"), default="pubsub",
                     help="pubsub drops frames on the wire (preferred); reqrep replies "
                          "immediately so the sender is still never blocked by inference")
    src.add_argument("--recv-timeout-ms", type=int, default=500,
                     help="socket recv timeout; bounds how long shutdown takes")
    src.add_argument("--raw", action="store_true",
                     help="sender uses send_image (ndarray) instead of send_jpg")

    m = p.add_argument_group("model")
    m.add_argument("--model", default="yolo26n-pose.pt",
                   help="ultralytics pose checkpoint or exported .engine")
    m.add_argument("--imgsz", type=int, default=640)
    m.add_argument("--device", default="cuda:0", help="cuda:0 | cpu")
    m.add_argument("--half", action="store_true", default=True,
                   help="FP16 inference (sent as quantize=16 on ultralytics 8.4+, "
                        "half=True on older releases; ignored for .engine models, "
                        "which carry their own precision)")
    m.add_argument("--no-half", dest="half", action="store_false")
    m.add_argument("--conf", type=float, default=None,
                   help=f"detection confidence (default {BASE_DEFAULTS['conf']}, "
                        f"{ROWING_DEFAULTS['conf']} with --rowing)")
    m.add_argument("--iou", type=float, default=0.7)
    m.add_argument("--max-det", type=int, default=None,
                   help=f"max people (default {BASE_DEFAULTS['max_det']}, "
                        f"{ROWING_DEFAULTS['max_det']} with --rowing)")
    m.add_argument("--warmup", type=int, default=3,
                   help="warmup passes so the first real tick isn't 2s long")

    s = p.add_argument_group("schedule")
    s.add_argument("--fps", type=float, default=15.0,
                   help="inference tick rate; independent of arrival rate")
    s.add_argument("--max-age-ms", type=float, default=0.0,
                   help="skip inference on frames older than this (0 = never skip)")
    s.add_argument("--quiet-stale", action="store_true",
                   help="suppress JSON on ticks with no fresh frame "
                        "(default: emit a stale record so the cadence is unbroken)")

    r = p.add_argument_group("resources (2 models on one Orin Nano)")
    r.add_argument("--mem-fraction", type=float, default=0.45,
                   help="cap this process's share of GPU memory; 0 to disable")
    r.add_argument("--torch-threads", type=int, default=2)
    r.add_argument("--cv-threads", type=int, default=1)

    r2 = p.add_argument_group("subject (side-view single rower)")
    r2.add_argument("--rowing", action="store_true",
                    help="profile for one side-on rower: single detection, lower "
                         "detection threshold, keypoint gating, near-side "
                         "selection, smoothing, and per-frame hip/trunk/knee/"
                         "elbow geometry. Every value it sets can be overridden "
                         "by passing that flag explicitly.")
    r2.add_argument("--side", choices=("auto", "left", "right", "off"), default=None,
                    help="which side faces the camera. auto low-passes the "
                         "left-vs-right keypoint confidence so it cannot flip on "
                         "one bad frame; pin it if you know the mounting.")
    r2.add_argument("--kp-conf", type=float, default=None,
                    help="per-keypoint confidence floor; below it x,y are emitted "
                         "as null instead of a guess (0 disables)")
    r2.add_argument("--keypoints", choices=("all", "body"), default=None,
                    help="'body' drops the 5 face points, useless in a side view. "
                         "The meta record always lists what is actually emitted.")
    r2.add_argument("--smooth-hz", type=float, default=None,
                    help="one-pole low-pass cutoff on keypoints, exploiting the "
                         "uniform tick. Lag ~= 1/(2*pi*fc) s. 0 disables. Requires "
                         "--max-det 1: the filter is identity-blind.")

    o = p.add_argument_group("output")
    o.add_argument("--gui", action="store_true", help="show annotated frames in a window")
    o.add_argument("--display-fps", type=float, default=15.0)
    o.add_argument("--gui-raw", action="store_true",
                   help="draw the unfiltered model output instead of the "
                        "smoothed/gated result; useful for seeing what the "
                        "filtering is actually doing")
    o.add_argument("--normalize", action="store_true",
                   help="emit coordinates normalized to [0,1] instead of pixels")
    o.add_argument("--publish", default="",
                   help="also republish every record on a ZMQ PUB socket, e.g. "
                        "tcp://*:5556. Carries the full record (pixels, "
                        "confidences, rowing block); consumers narrow it.")
    o.add_argument("--publish-connect", action="store_true",
                   help="connect the PUB socket instead of binding it")
    o.add_argument("--publish-hwm", type=int, default=10,
                   help="PUB high-water mark; over this, frames are dropped "
                        "rather than buffered")
    o.add_argument("--publish-meta-s", type=float, default=2.0,
                   help="resend the meta record this often, so subscribers that "
                        "join late still learn the keypoint names")
    o.add_argument("--no-stdout", action="store_true",
                   help="suppress the JSONL on stdout (use with --publish)")
    o.add_argument("--out-queue", type=int, default=256,
                   help="buffer this many JSON records for a background stdout "
                        "writer, so a slow consumer cannot stall the tick. On "
                        "overflow the oldest is dropped (gaps show as jumps in "
                        "'tick'). 0 writes synchronously from the tick.")
    o.add_argument("--stats-interval", type=float, default=5.0,
                   help="stderr stats period in seconds; 0 to disable")

    e = p.add_argument_group("tooling")
    e.add_argument("--check-env", action="store_true",
                   help="report torch/TensorRT/CUDA resolution and exit; use this "
                        "when an export or model load fails")
    e.add_argument("--allow-autoinstall", action="store_true",
                   help="let ultralytics pip-install missing export deps; off by "
                        "default because on Jetson it pulls desktop CUDA wheels "
                        "that shadow JetPack's")
    e.add_argument("--export-engine", action="store_true",
                   help="export --model to TensorRT and exit")
    e.add_argument("--trt-workspace", type=int, default=2, help="TensorRT workspace (GB)")
    args = p.parse_args(argv)

    defaults = ROWING_DEFAULTS if args.rowing else BASE_DEFAULTS
    explicit = {k for k in defaults if getattr(args, k) is not None}
    for key, value in defaults.items():
        if getattr(args, key) is None:
            setattr(args, key, value)

    if args.smooth_hz > 0 and args.max_det != 1:
        # only an error if the user asked for both; if the profile supplied the
        # smoothing, quietly stand down rather than refusing to start
        if "smooth_hz" in explicit:
            p.error("--smooth-hz needs --max-det 1; the filter carries no "
                    "identity, so with several people it would blend them")
        args.smooth_hz = 0.0
        log(f"[warn] smoothing off: --max-det {args.max_det} means detections "
            f"carry no identity for the filter to follow")
    if args.smooth_hz >= args.fps / 2:
        p.error(f"--smooth-hz {args.smooth_hz} must stay below half the tick rate "
                f"({args.fps / 2:g} Hz at --fps {args.fps:g})")
    if args.rowing and args.normalize:
        log("[warn] --normalize scales x and y by different factors, so angles "
            "computed from the emitted keypoints will be skewed. The 'rowing' "
            "block is computed in pixels and is unaffected.")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)

    # keep BLAS/OpenCV from grabbing every core; two of these run side by side
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(var, str(args.torch_threads))

    if args.check_env:
        return check_env(args)

    if args.export_engine:
        return export_engine(args)

    check_model(args)

    import cv2

    cv2.setNumThreads(args.cv_threads)

    def on_signal(signum, _frame):
        log(f"[main] signal {signum}, shutting down")
        STOP.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    slot = LatestSlot()
    display = LatestSlot() if args.gui else None

    global _EMITTER, _PUBLISHER, _NO_STDOUT
    _NO_STDOUT = args.no_stdout
    if args.publish:
        _PUBLISHER = Publisher(args.publish, args.publish_hwm,
                               args.publish_meta_s,
                               bind=not args.publish_connect)
        _PUBLISHER.start()
        _PUBLISHER.ready.wait(timeout=2.0)

    if args.no_stdout:
        _EMITTER = None
    elif args.out_queue > 0:
        _EMITTER = Emitter(args.out_queue)
        _EMITTER.start()

    inf = Inferencer(args, slot, display)
    inf.start()
    inf.ready.wait()          # don't start receiving until the model is warm
    if STOP.is_set():
        return 1

    recv = Receiver(args, slot)
    recv.start()

    emit({"type": "meta", "t": time.time(), "model": args.model, "imgsz": args.imgsz,
          "device": args.device, "fps": args.fps, "source": args.connect,
          "mode": args.mode, "normalized": bool(args.normalize),
          "profile": "rowing" if args.rowing else "default",
          "conf": args.conf, "max_det": args.max_det, "kp_conf": args.kp_conf,
          "side": args.side, "smooth_hz": args.smooth_hz,
          "keypoints": [COCO_KEYPOINTS[i] for i in inf.emit_idx]})

    threading.Thread(target=reporter, args=(args, recv, inf, slot),
                     name="stats", daemon=True).start()

    try:
        if args.gui:
            gui_loop(args, display)   # cv2 GUI must own the main thread
        else:
            while not STOP.wait(0.5):
                if not inf.is_alive():
                    break
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        inf.join(timeout=3.0)
        recv.join(timeout=3.0)
        if _EMITTER is not None:
            _EMITTER.close()      # flush what is queued before exiting
    log("[main] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

