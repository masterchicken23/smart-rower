#!/usr/bin/env python3
"""
AprilTag ingest over ZMQ, from vision/april-detect.

The wire contract is owned by the detector and documented in full in
vision/april-detect/README.md, "Output contract". The short version:

Transport
    One ZMQ PUB per camera container, each *binding* tcp://*:556N. The funnel
    holds one SUB socket and connect()s it to every detector, subscribed to the
    prefix `apriltag`, which receives both topics. Two-frame multipart
    [topic, compact_json]; dispatch is on the exact topic.

    The publishers drop at their high-water mark rather than buffer, so a slow
    funnel sees gaps rather than lag. `tick` is gapless per detector, so those
    gaps are countable.

Topics
    apriltag        one record per processed frame, including frames with no
                    tag in view (`n: 0`), so "nothing seen" is distinguishable
                    from "detector down"
    apriltag_meta   camera model and conventions, every ~2 s

Mapping onto the funnel's model
    Each camera watches one rower, and FUNNEL_APRILTAG_CAMERAS says which
    (`cam1:1,cam2:2,...`). The detector does not know seats, and should not:
    the funnel already owns them, and a camera moved to another seat is a
    config change here rather than a redeploy of the detector.

    key       StreamKey("rower", seat, "apriltag")
    t_src     rec["t"], the frame's capture time already on this machine's
              wall clock, so no ClockEstimator
    seq       rec["tick"], so n_gap counts records lost between detector and
              funnel; a container restart resets it, which store.py already
              recognises as a restart rather than a loss
    dev       rec["cam"]
    values    the record, whole and untouched

    A camera that is not in the map is not ingested: filing its tags under a
    guessed seat would be silently wrong. Its records are counted per camera
    and listed under this source in /health, so it is visible rather than
    missing -- the camera equivalent of boat/unassigned.

Unlike MQTT, the envelope fields are left *in* `values`: these are the raw
detections, and a client reading them should get exactly what the detector
published. Turning tag poses into a seat position is a derivation and belongs
in compute/, not here.

Meta records are not samples. They arrive more slowly than FUNNEL_MAX_AGE_MS,
so as a stream they would always read stale; they are kept beside the store
instead (`StreamStore.apriltag_meta`, by seat) and served by
/rower/{seat}/apriltag/meta.

Decoding is split out as plain functions so the contract can be tested without
sockets -- see tests/test_apriltag.py.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from .. import log
from ..config import Settings
from ..models import Sample, StreamKey
from ..recorder import Recorder
from ..store import StreamStore
from .base import BaseSource

TOPIC_TAGS = b"apriltag"
TOPIC_META = b"apriltag_meta"
STREAM = "apriltag"


def decode_message(parts: list[bytes]) -> Optional[tuple[bytes, dict]]:
    """[topic, json] -> (topic, record). None if it is not that shape."""
    if len(parts) != 2:
        return None
    try:
        obj = json.loads(parts[1])
    except Exception:                   # noqa: BLE001
        return None
    if not isinstance(obj, dict):
        return None
    return parts[0], obj


def record_cam(rec: dict) -> Optional[str]:
    """The camera id a record belongs to, if it names one usable in a path."""
    cam = rec.get("cam")
    if not isinstance(cam, str) or not cam or "/" in cam:
        return None
    return cam


def to_sample(rec: dict, t_recv: float) -> Sample:
    """An `apriltag` record -> Sample. The record itself is the payload."""
    tick = rec.get("tick")
    seq = (int(tick) % (2 ** 32)
           if isinstance(tick, (int, float)) and not isinstance(tick, bool)
           else None)
    t = rec.get("t")
    t_src = (float(t) if isinstance(t, (int, float))
             and not isinstance(t, bool) and t > 0 else None)
    return Sample(t_recv=t_recv, t_src=t_src, seq=seq, values=rec,
                  dev=record_cam(rec))


class ZmqAprilTagSource(BaseSource):
    """Subscribes to every configured detector on one SUB socket.

    Uses pyzmq's asyncio sockets, so receiving is an await on the shared event
    loop like MQTT's, and cancellation interrupts it directly. Everything from
    receipt to buffer insert is synchronous, as in MqttSource.
    """

    name = "apriltag"

    def __init__(self, store: StreamStore, settings: Settings,
                 recorder: Optional[Recorder] = None) -> None:
        super().__init__(store, recorder)
        self.settings = settings
        self.seats = settings.camera_seats
        self.n_meta = 0
        self.n_errors = 0
        self.unmapped: dict[str, int] = {}
        """Records per camera that has no seat in FUNNEL_APRILTAG_CAMERAS."""
        self.last_error: Optional[str] = None

    def stats(self) -> dict[str, Any]:
        s = super().stats()
        s.update({
            "connect": self.settings.apriltag_endpoints,
            "meta": self.n_meta,
            "errors": self.n_errors,
            "cameras": {cam: f"rower/{seat}/{STREAM}"
                        for cam, seat in sorted(self.seats.items())},
            "unmapped": dict(sorted(self.unmapped.items())),
            "last_error": self.last_error,
        })
        return s

    async def run(self) -> None:
        # Deferred, so the funnel imports and tests without pyzmq unless this
        # source is enabled.
        import zmq
        import zmq.asyncio

        endpoints = self.settings.apriltag_endpoints
        if not endpoints:
            log.warn("[apriltag] enabled with no endpoints; nothing to do")
            return
        if not self.seats:
            log.warn("[apriltag] FUNNEL_APRILTAG_CAMERAS is empty; every "
                     "camera will be ignored until it is mapped to a seat")

        ctx = zmq.asyncio.Context.instance()
        sock = ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RCVHWM, self.settings.apriltag_rcvhwm)
        sock.setsockopt(zmq.SUBSCRIBE, TOPIC_TAGS)     # prefix: meta too
        try:
            # connect() never fails on an absent peer: ZMQ reconnects in the
            # background, so a detector started after the funnel just appears.
            for ep in endpoints:
                sock.connect(ep)
            log.info(f"[apriltag] subscribed to {', '.join(endpoints)}")
            while True:
                try:
                    parts = await sock.recv_multipart()
                except zmq.ZMQError as e:
                    # Anything else (a Proactor loop on Windows, say) is a
                    # setup fault and should end the task, not spin here.
                    self.last_error = repr(e)
                    self.n_errors += 1
                    if self.n_errors <= 3 or self.n_errors % 30 == 0:
                        log.warn(f"[apriltag] receive failed: {e!r}")
                    await asyncio.sleep(1.0)
                    continue
                self._handle(parts)
        except asyncio.CancelledError:
            log.info("[apriltag] stopped")
            raise
        finally:
            sock.close(linger=0)

    def _handle(self, parts: list[bytes]) -> None:
        msg = decode_message(parts)
        if msg is None:
            self._bad(None, parts)
            return
        topic, rec = msg
        cam = record_cam(rec)

        if topic == TOPIC_META:
            if cam is None:
                # A detector publishes meta before its first frame, when it
                # may not yet know its camera's name. Nothing to file it under.
                return
            seat = self.seats.get(cam)
            if seat is None:
                return                      # counted on its records instead
            self.n_meta += 1
            self.store.apriltag_meta[seat] = dict(rec, t_recv=time.monotonic())
            return

        if topic != TOPIC_TAGS:
            self.n_unknown += 1
            if self.n_unknown <= 3:
                log.warn(f"[apriltag] ignoring unrecognised topic {topic[:32]!r}")
            return

        if cam is None:
            self._bad(None, parts)
            return
        seat = self.seats.get(cam)
        if seat is None:
            n = self.unmapped.get(cam, 0) + 1
            self.unmapped[cam] = n
            if n == 1:
                log.warn(f"[apriltag] camera {cam!r} has no seat in "
                         f"FUNNEL_APRILTAG_CAMERAS; ignoring its records")
            return
        key = StreamKey("rower", seat, STREAM)
        if not isinstance(rec.get("tags"), list):
            self._bad(key, parts)
            return
        self.emit(key, to_sample(rec, time.monotonic()))

    def _bad(self, key: Optional[StreamKey], parts: list[bytes]) -> None:
        self.n_bad += 1
        self.store.mark_bad(key)
        if self.n_bad <= 3:
            head = parts[-1][:64] if parts else b""
            log.warn(f"[apriltag] undecodable message: {head!r}")
