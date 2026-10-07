#!/usr/bin/env python3
"""
The pipeline: receive -> decode -> undistort -> detect -> pose -> publish.

    [recv thread]  ZMQ SUB. Does nothing but recv and stamp the arrival time,
                   then drops the frame into a LatestSlot. Stamping here, on a
                   thread that is always parked in recv, is what makes `recv`
                   the moment the frame reached this process -- not the
                   moment the processing loop got round to it.
    [main thread]  takes the newest frame, runs the stages, publishes one
                   record. JPEG decode, remap and the CPU detector all
                   release the GIL, so the recv thread is never starved.

Latest-wins rather than a queue, the same trade as pose_stream.py's
LatestSlot and the funnel's drop-oldest buffers: if processing falls behind,
a stale frame is worth less than the next one, and a queue would turn a
momentary overrun into permanent lag. Frames overwritten before they were
processed are counted and reported as `skipped`, so falling behind is visible.

Unlike pose_stream.py this loop is event-driven, not on a fixed tick. That one
ticks on a grid because it low-pass filters keypoints, which needs uniform
spacing; this one does no temporal filtering, so it processes each frame the
moment it arrives and adds no scheduling latency. Downstream gets the capture
timestamp of every frame and can resample on its own grid -- which is exactly
what the funnel's compute loop does.

`process()` is socket-free, so tests drive it directly.
"""

from __future__ import annotations

import math
import os
import threading
import time
from typing import Any, Optional

import numpy as np

from . import calib as calib_mod
from . import detectors, log, pose, wire
from .clock import ClockEstimator, stamp
from .config import Settings
from .wire import r

VERSION = "0.1.0"

CONVENTIONS = {
    "camera_frame": "OpenCV: +x right, +y down, +z out of the lens",
    "tag_frame": "origin at tag centre; +x tag right, +y tag down, +z into the "
                 "tag; upright tag facing the camera -> identity",
    "pos_m": "tag origin in the camera frame, metres",
    "quat": "[w, x, y, z], rotates tag-frame vectors into the camera frame",
    "euler_deg": "[rx, ry, rz], R = Rz @ Ry @ Rx, degrees; display only",
    "corners_px": "corrected-image pixels (intrinsics K_rect), AprilTag order "
                  "(tag-frame -x+y, +x+y, +x-y, -x-y)",
    "ts": "host wall clock, unix seconds",
    "ms": "stage durations, milliseconds",
}


class LatestSlot:
    """One-item mailbox: the writer never blocks, the reader never backlogs."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._item: Optional[tuple] = None
        self.overwritten = 0

    def put(self, item: tuple) -> None:
        with self._cv:
            if self._item is not None:
                self.overwritten += 1
            self._item = item
            self._cv.notify()

    def take(self, timeout: float) -> Optional[tuple]:
        with self._cv:
            if self._item is None:
                self._cv.wait(timeout)
            item, self._item = self._item, None
            return item


class Receiver(threading.Thread):
    """Does nothing but recv. ZMQ reconnects a SUB by itself, so a camera that
    reboots simply resumes; there is no reconnect logic to get wrong."""

    def __init__(self, s: Settings, slot: LatestSlot, stop: threading.Event) -> None:
        super().__init__(name="recv", daemon=True)
        self.s, self.slot, self.stop = s, slot, stop
        self.n_recv = 0
        self.t_last: Optional[float] = None

    def run(self) -> None:
        import zmq

        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.RCVHWM, self.s.recv_hwm)
        sock.setsockopt(zmq.RCVTIMEO, self.s.recv_timeout_ms)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.SUBSCRIBE, self.s.source_topic.encode())
        sock.connect(self.s.source)
        log.info(f"[recv] SUB connect {self.s.source}")
        try:
            while not self.stop.is_set():
                try:
                    parts = sock.recv_multipart()
                except zmq.Again:
                    continue
                t_mono, t_wall = time.perf_counter(), time.time()
                self.n_recv += 1
                self.t_last = t_mono
                self.slot.put((parts, t_mono, t_wall))
        except Exception as e:                      # noqa: BLE001
            if not self.stop.is_set():
                log.error(f"[recv] stopped: {e!r}")
                self.stop.set()
        finally:
            sock.close(0)


# --------------------------------------------------------------------------- #
class Stats:
    """Per-interval counters and timings for the stderr stats line."""

    STAGES = ("decode", "undistort", "detect", "pose", "proc")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.n = 0
        self.n_tags = 0
        self.n_bad = 0
        self.n_err = 0
        self.busy_s = 0.0
        self.sums = dict.fromkeys(self.STAGES, 0.0)
        self.maxs = dict.fromkeys(self.STAGES, 0.0)
        self.e2e: list[float] = []
        self.net: list[float] = []
        self.total: list[float] = []

    def add(self, rec: dict[str, Any], busy_s: float) -> None:
        self.n += 1
        self.n_tags += rec["n"]
        self.busy_s += busy_s
        ms = rec["ms"]
        for k in self.STAGES:
            v = ms.get(k) or 0.0
            self.sums[k] += v
            self.maxs[k] = max(self.maxs[k], v)
        for k, lst in (("e2e", self.e2e), ("net", self.net), ("total", self.total)):
            if ms.get(k) is not None:
                lst.append(ms[k])


def _pct(v: list[float], p: float) -> Optional[float]:
    if not v:
        return None
    v = sorted(v)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


# --------------------------------------------------------------------------- #
class Pipeline:
    def __init__(self, s: Settings, detector: Any = None) -> None:
        import cv2

        self.s = s
        self.det = detector if detector is not None else detectors.build(s)
        self.use_native_pose = (s.pose_source == "detector"
                                or not self.det.corner_order_verified)
        if self.use_native_pose and s.undistort == "points":
            raise SystemExit("[main] APRIL_UNDISTORT=points needs pose from "
                             "corners (APRIL_POSE_SOURCE=ippe and the cpu "
                             "detector): a detector's own pose would be "
                             "solved on the distorted image")
        if self.use_native_pose and s.tag_sizes:
            log.info("[pose] detector pose with per-id sizes: translation is "
                     "rescaled per id, rotation is size-independent")
        base: Optional[calib_mod.Calibration] = None
        if s.calib_path:
            base = calib_mod.from_file(s.calib_path)
        elif s.camera_matrix:
            base = calib_mod.from_inline(s.camera_matrix, s.dist_coeffs,
                                         s.dist_model, s.calib_size)
        self.camera = calib_mod.CameraModel(base, s.undistort, s.undistort_alpha,
                                            s.hfov_deg)
        self.decode_flag = {
            1: cv2.IMREAD_GRAYSCALE,
            2: cv2.IMREAD_REDUCED_GRAYSCALE_2,
            4: cv2.IMREAD_REDUCED_GRAYSCALE_4,
            8: cv2.IMREAD_REDUCED_GRAYSCALE_8,
        }[s.decode_scale]
        self.sizes = s.size_by_id
        self.allow = s.id_allow
        self.clock = ClockEstimator()
        self.tick = 0
        self.cam = s.camera_id or None
        self.frame_size: Optional[tuple[int, int]] = None
        self.K_rect: Optional[np.ndarray] = None
        self.calib_desc: Optional[dict] = None
        self.started = time.time()
        self.stats = Stats()

    # -- one frame ----------------------------------------------------------- #
    def process(self, parts: list[bytes], t_recv_mono: float, t_recv_wall: float,
                skipped: int = 0) -> Optional[dict[str, Any]]:
        """Run every stage on one received message; return the record to
        publish, or None if the message held no decodable frame."""
        import cv2

        def wall(t_mono: float) -> float:
            # One wall-clock anchor per frame; everything after it is placed
            # by the monotonic clock, so a wall-clock step mid-frame cannot
            # produce a negative stage time.
            return t_recv_wall + (t_mono - t_recv_mono)

        t0 = time.perf_counter()
        got = wire.decode_frame(parts)
        if got is None:
            self.stats.n_bad += 1
            return None
        hdr, jpeg = got
        gray = cv2.imdecode(np.frombuffer(jpeg, np.uint8), self.decode_flag)
        if gray is None:
            self.stats.n_bad += 1
            return None
        t1 = time.perf_counter()

        h, w = gray.shape[:2]
        und = self.camera.for_size(w, h)
        rect = und.image(gray)
        if (w, h) != self.frame_size:
            self.frame_size = (w, h)
            self.K_rect = und.K_rect
            self.calib_desc = und.calib.describe()
        t2 = time.perf_counter()

        dets = self.det.detect(rect, und.K_rect)
        t3 = time.perf_counter()

        tags = []
        for d in dets:
            if self.allow and d.id not in self.allow:
                continue
            if d.hamming > self.s.max_hamming:
                continue
            tag = self._tag(d, und)
            if tag is not None:
                tags.append(tag)
        t4 = time.perf_counter()

        st = stamp(hdr, self.clock, t_recv_mono, t_recv_wall)
        if self.cam is None:
            self.cam = hdr.cam or "cam"
        self.tick += 1
        t_pub_mono = time.perf_counter()
        t_pub = wall(t_pub_mono)

        def dms(a: Optional[float], b: Optional[float]) -> Optional[float]:
            return None if a is None or b is None else r((b - a) * 1000.0, 3)

        rec = {
            "type": "apriltag",
            "v": wire.SCHEMA,
            "cam": self.cam,
            "tick": self.tick,
            "seq": hdr.seq,
            "skipped": skipped,
            "t": r(st.t_cap if st.t_cap is not None else t_recv_wall, 6),
            "clock": st.clock,
            "w": w, "h": h,
            "ts": {
                "cap": r(st.t_cap, 6),
                "send": r(st.t_send, 6),
                "recv": r(t_recv_wall, 6),
                "start": r(wall(t0), 6),
                "pub": r(t_pub, 6),
            },
            "ms": {
                "enc": dms(st.t_cap, st.t_send),
                "net": dms(st.t_send, t_recv_wall),
                "queue": dms(t_recv_mono, t0),
                "decode": dms(t0, t1),
                "undistort": dms(t1, t2),
                "detect": dms(t2, t3),
                "pose": dms(t3, t4),
                "proc": dms(t0, t_pub_mono),
                "total": dms(t_recv_mono, t_pub_mono),
                "e2e": dms(st.t_cap, t_pub),
            },
            "n": len(tags),
            "tags": tags,
        }
        self.stats.add(rec, t_pub_mono - t0)
        return rec

    def _tag(self, d: detectors.Detection,
             und: calib_mod.Undistorter) -> Optional[dict[str, Any]]:
        size = self.sizes.get(d.id, self.s.tag_size_m)
        corners = und.points(d.corners)
        err = amb = None
        if self.use_native_pose:
            if d.R is None or d.t is None:
                return None
            R = d.R
            t = d.t * (size / d.size_m) if d.size_m else d.t
        else:
            p = pose.solve(corners, size, und.K_rect)
            if p is None:
                return None
            R, t, err, amb = p["R"], p["t"], p["err_px"], p["ambiguity"]
        c = corners.mean(axis=0)
        return {
            "id": d.id,
            "fam": d.family,
            "size_m": size,
            "pos_m": [r(v, 4) for v in t],
            "quat": [r(v, 5) for v in pose.R_to_quat(R)],
            "euler_deg": [r(v, 2) for v in pose.R_to_euler_deg(R)],
            "dist_m": r(math.sqrt(float(t @ t)), 4),
            "center_px": [r(c[0], 2), r(c[1], 2)],
            "corners_px": [[r(x, 2), r(y, 2)] for x, y in corners],
            "hamming": d.hamming,
            "margin": r(d.margin, 2),
            "err_px": r(err, 3),
            "ambiguity": r(amb, 3),
        }

    def meta(self) -> dict[str, Any]:
        s = self.s
        return {
            "type": "apriltag_meta",
            "v": wire.SCHEMA,
            "version": VERSION,
            "cam": self.cam or s.camera_id or None,
            "t": r(time.time(), 6),
            "started": r(self.started, 3),
            "source": s.source,
            "detector": self.det.describe(),
            "families": s.family_list,
            "tag_size_m": s.tag_size_m,
            "tag_sizes": {str(k): v for k, v in self.sizes.items()},
            "tag_ids": sorted(self.allow) or None,
            "max_hamming": s.max_hamming,
            "decimate": s.decimate if self.det.name == "cpu" else None,
            "decode_scale": s.decode_scale,
            "pose_source": "detector" if self.use_native_pose else "ippe",
            "undistort": s.undistort,
            "frame": list(self.frame_size) if self.frame_size else None,
            "K_rect": ([r(v, 4) for v in self.K_rect.ravel()]
                       if self.K_rect is not None else None),
            "calib": self.calib_desc,
            "clock_resets": self.clock.n_resets,
            "conventions": CONVENTIONS,
        }

    # -- the loop ------------------------------------------------------------ #
    def run(self, stop: threading.Event) -> None:
        import zmq

        s = self.s
        slot = LatestSlot()
        recv = Receiver(s, slot, stop)

        ctx = zmq.Context.instance()
        pub = ctx.socket(zmq.PUB)
        pub.setsockopt(zmq.SNDHWM, s.out_hwm)       # drop rather than buffer
        pub.setsockopt(zmq.LINGER, 0)
        if s.out_bind:
            pub.bind(s.out_address)
        else:
            pub.connect(s.out_address)
        log.info(f"[pub] PUB {'bind' if s.out_bind else 'connect'} "
                 f"{s.out_address} topics {wire.TOPIC_TAGS.decode()}, "
                 f"{wire.TOPIC_META.decode()}")
        log.info(f"[det] {self.det.describe()} families={s.families} "
                 f"pose={'detector' if self.use_native_pose else 'ippe'} "
                 f"undistort={s.undistort} decimate={s.decimate} "
                 f"threads={s.threads}")

        recv.start()
        next_meta = 0.0
        next_stats = time.perf_counter() + s.stats_interval_s
        next_beat = 0.0
        last_seen_skipped = 0
        t_warned = time.perf_counter()
        t_interval = time.perf_counter()
        n_recv_mark = 0

        def send(topic: bytes, obj: dict) -> None:
            try:
                pub.send_multipart(wire.encode(topic, obj), flags=zmq.NOBLOCK)
            except zmq.Again:
                pass                                # PUB at HWM: dropped
            except Exception as e:                  # noqa: BLE001
                log.warn(f"[pub] send failed: {e!r}")

        try:
            while not stop.is_set():
                item = slot.take(timeout=0.25)
                now = time.perf_counter()

                if item is not None:
                    parts, t_mono, t_wall = item
                    skipped = slot.overwritten - last_seen_skipped
                    last_seen_skipped = slot.overwritten
                    try:
                        rec = self.process(parts, t_mono, t_wall, skipped)
                    except Exception as e:          # noqa: BLE001
                        # A bad frame must not take the detector down.
                        self.stats.n_err += 1
                        if self.stats.n_err <= 3:
                            log.error(f"[proc] frame failed: {e!r}")
                        rec = None
                    if rec is not None and (rec["n"] or s.emit_empty):
                        send(wire.TOPIC_TAGS, rec)
                    now = time.perf_counter()
                elif (recv.t_last is None or now - recv.t_last > s.no_frames_warn_s) \
                        and now - t_warned > s.no_frames_warn_s:
                    log.warn(f"[recv] no frames from {s.source} in "
                             f"{s.no_frames_warn_s:g}s")
                    t_warned = now

                if now >= next_meta:
                    next_meta = now + s.meta_period_s
                    send(wire.TOPIC_META, self.meta())
                if now >= next_beat:
                    next_beat = now + 1.0
                    self._heartbeat()
                if s.stats_interval_s > 0 and now >= next_stats:
                    dt = now - t_interval
                    self._report(dt, recv.n_recv - n_recv_mark)
                    n_recv_mark = recv.n_recv
                    t_interval = now
                    next_stats = now + s.stats_interval_s
        finally:
            stop.set()
            recv.join(timeout=2.0)
            pub.close(0)
            self.det.close()

    def _heartbeat(self) -> None:
        p = self.s.heartbeat_path
        if not p:
            return
        try:
            with open(p, "a"):
                pass
            os.utime(p, None)
        except OSError:
            pass                    # a missing heartbeat must not stop detection

    def _report(self, dt: float, n_in: int) -> None:
        st = self.stats
        n = max(st.n, 1)

        def f(v: Optional[float]) -> str:
            return "  -  " if v is None else f"{v:5.1f}"

        stages = " ".join(f"{k} {st.sums[k] / n:4.1f}" for k in Stats.STAGES)
        log.info(f"[stats] in {n_in / dt:5.1f}/s out {st.n / dt:5.1f}/s "
                 f"busy {100 * st.busy_s / dt:3.0f}%  "
                 f"skipped {max(n_in - st.n - st.n_bad, 0)}  bad {st.n_bad}  "
                 f"err {st.n_err}  tags/frame {st.n_tags / n:.2f} | ms: {stages} "
                 f"(proc max {st.maxs['proc']:.1f}) | e2e p50 {f(_pct(st.e2e, 50))} "
                 f"p95 {f(_pct(st.e2e, 95))}  net p50 {f(_pct(st.net, 50))}  "
                 f"recv->pub p95 {f(_pct(st.total, 95))}")
        st.reset()
