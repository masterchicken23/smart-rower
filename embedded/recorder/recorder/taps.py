#!/usr/bin/env python3
"""
The receivers. One thread per source, each doing nothing but receive, stamp
and hand off:

    MqttTap      paho-mqtt's own network thread, subscribed to the boat's
                 broker beside the funnel. A second subscriber costs the
                 sensors nothing: mosquitto fans out on the Jetson.
    VideoTap     one thread per camera, a ZMQ SUB on april-detect's raw frame
                 tee (or straight on the camera).
    AprilTagTap  one thread, one SUB connected to every detector.

Stamping in a thread that is always parked in recv is what makes the stamp the
moment of arrival -- the pattern of april-detect's Receiver. Disk writes
happen elsewhere (writer.py), so a slow card can delay a write but never a
stamp.

Taps run for the life of the container, recording or not. That way
/recorder/status shows which sources are alive before you press start, and a
session loses nothing to sockets still connecting.

Every device's sequence counter is tracked with the funnel's restart-aware
rule (funnel/store.py, StreamBuffer.push): a forward jump is a counted loss, a
backward one is a sender restart, never four billion lost samples.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Optional

from . import log
from .config import Settings
from .format import (
    U32,
    AprilMsg,
    FrameMsg,
    Item,
    MqttMsg,
    frame_header,
    json_obj,
    num,
    split_tee,
    stamp,
)

Sink = Callable[[Item], None]

REC_FRESH_NS = 2_000_000_000
"""A camera's `rec` confirmation older than this is shown as null."""
REC_SESSIONS_KEPT = 16


def _seq(v: Any) -> Optional[int]:
    f = num(v)
    return None if f is None else int(f) % U32


class Device:
    """Counters for one sender: an MQTT topic, a camera, a detector."""

    __slots__ = ("n", "nbytes", "n_gap", "n_restart", "last_seq", "last_tm",
                 "_recent")

    def __init__(self) -> None:
        self.n = 0
        self.nbytes = 0
        self.n_gap = 0
        self.n_restart = 0
        self.last_seq: Optional[int] = None
        self.last_tm: Optional[int] = None
        self._recent: deque = deque(maxlen=64)

    def seen(self, tm: int, nbytes: int, seq: Optional[int]) -> None:
        if seq is not None:
            if self.last_seq is not None:
                gap = (seq - self.last_seq) % U32
                if 1 < gap < U32 // 2:
                    self.n_gap += gap - 1
                elif gap >= U32 // 2:
                    self.n_restart += 1
            self.last_seq = seq
        self.n += 1
        self.nbytes += nbytes
        self.last_tm = tm
        self._recent.append(tm)

    def view(self, now_tm: int) -> dict[str, Any]:
        r = self._recent
        span = (r[-1] - r[0]) / 1e9 if len(r) >= 2 else 0.0
        return {
            "n": self.n,
            "rate_hz": round((len(r) - 1) / span, 2) if span > 0 else None,
            "age_ms": (None if self.last_tm is None
                       else round((now_tm - self.last_tm) / 1e6, 1)),
            "gaps": self.n_gap,
            "restarts": self.n_restart,
            "mb": round(self.nbytes / 1e6, 2),
        }


class Counters:
    """Devices by name. Written by a receiver thread and read by the HTTP
    handlers, hence the lock -- held only for dict-sized moments."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dev: dict[str, Device] = {}

    def seen(self, key: str, tm: int, nbytes: int, seq: Optional[int]) -> None:
        with self._lock:
            d = self._dev.get(key)
            if d is None:
                d = self._dev[key] = Device()
            d.seen(tm, nbytes, seq)

    def view(self) -> dict[str, dict[str, Any]]:
        now = time.perf_counter_ns()
        with self._lock:
            return {k: d.view(now) for k, d in sorted(self._dev.items())}

    def totals(self) -> dict[str, tuple[int, int, int, int]]:
        """(n, gaps, restarts, bytes) per device: a session's baseline."""
        with self._lock:
            return {k: (d.n, d.n_gap, d.n_restart, d.nbytes)
                    for k, d in self._dev.items()}


def diff(now: dict[str, tuple[int, int, int, int]],
         base: dict[str, tuple[int, int, int, int]]) -> dict[str, dict[str, int]]:
    """What happened between a baseline and now, per device."""
    out = {}
    for k, (n, g, r, b) in sorted(now.items()):
        n0, g0, r0, b0 = base.get(k, (0, 0, 0, 0))
        if n - n0:
            out[k] = {"n": n - n0, "gaps": g - g0, "restarts": r - r0,
                      "bytes": b - b0}
    return out


class Tap:
    name = "tap"

    def __init__(self, sink: Sink) -> None:
        self.sink = sink
        self.counters = Counters()
        self.n_errors = 0
        self.last_error: Optional[str] = None

    @property
    def key(self) -> str:
        """Unique among the taps; video taps add their camera."""
        return self.name

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def status(self) -> dict[str, Any]:
        return {"devices": self.counters.view(), "errors": self.n_errors,
                "last_error": self.last_error}


# ---- MQTT --------------------------------------------------------------- #

class MqttTap(Tap):
    """Every message on the broker, payload untouched.

    Subscribed at QoS 1 so a message is recorded at the QoS it was published
    with (the delivered QoS is the lower of the two): samples at 0, the
    retained status topics at 1. The player publishes each one back at the
    QoS recorded.
    """

    name = "mqtt"

    def __init__(self, s: Settings, sink: Sink) -> None:
        super().__init__(sink)
        self.s = s
        self.connected = False
        self.n_connects = 0
        self._client: Any = None

    def start(self) -> None:
        import paho.mqtt.client as mqtt

        cid = self.s.mqtt_client_id or f"recorder-{os.getpid()}"
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid,
                        clean_session=True)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(1, 5)
        c.connect_async(self.s.mqtt_host, self.s.mqtt_port,
                        keepalive=self.s.mqtt_keepalive_s)
        c.loop_start()
        self._client = c
        log.info(f"[mqtt] connecting {self.s.mqtt_host}:{self.s.mqtt_port} "
                 f"topics {', '.join(self.s.topics)}")

    def stop(self) -> None:
        if self._client is not None:
            self._client.disconnect()
            self._client.loop_stop()
            self._client = None

    def resubscribe(self) -> None:
        """Subscribe again. The broker answers a repeated subscription by
        re-sending every retained message under it (MQTT 3.1.1, 3.8.4), so a
        new session starts with the current `rower/<seat>/status` presence."""
        if self._client is not None and self.connected:
            self._client.subscribe([(t, 1) for t in self.s.topics])

    def status(self) -> dict[str, Any]:
        st = super().status()
        st.update({"broker": f"{self.s.mqtt_host}:{self.s.mqtt_port}",
                   "topics": self.s.topics, "connected": self.connected,
                   "connects": self.n_connects})
        return st

    # paho callbacks, on paho's network thread
    def _on_connect(self, client: Any, _ud: Any, _flags: Any, rc: Any,
                    _props: Any = None) -> None:
        if getattr(rc, "is_failure", False):
            self.last_error = f"connect refused: {rc}"
            log.warn(f"[mqtt] {self.last_error}")
            return
        self.connected = True
        self.n_connects += 1
        client.subscribe([(t, 1) for t in self.s.topics])
        log.info(f"[mqtt] connected (#{self.n_connects})")

    def _on_disconnect(self, _client: Any, _ud: Any, _flags: Any, rc: Any,
                       _props: Any = None) -> None:
        if self.connected:
            log.warn(f"[mqtt] disconnected: {rc}; paho will reconnect")
        self.connected = False

    def _on_message(self, _client: Any, _ud: Any, msg: Any) -> None:
        tm, tw = stamp()                    # first, before anything else
        try:
            payload = bytes(msg.payload)
            obj = json_obj(payload)
            seq = _seq(obj.get("seq")) if obj is not None else None
            self.counters.seen(msg.topic, tm, len(payload), seq)
            self.sink(MqttMsg(tm, tw, msg.topic, int(msg.qos), bool(msg.retain),
                              payload))
        except Exception as e:              # noqa: BLE001
            self.n_errors += 1
            self.last_error = repr(e)


# ---- ZMQ ---------------------------------------------------------------- #

def _sub_socket(hwm: int, subscribe: bytes) -> Any:
    import zmq

    sock = zmq.Context.instance().socket(zmq.SUB)
    sock.setsockopt(zmq.RCVHWM, hwm)
    sock.setsockopt(zmq.RCVTIMEO, 250)          # so stop is noticed promptly
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.SUBSCRIBE, subscribe)
    return sock


class VideoTap(Tap):
    """One camera. Accepts the tee's `[b"tee1", stamp, *parts]` and also a
    camera's own `[{"msg": header}, jpeg]`, so it can be pointed straight at
    a camera when april-detect is not running."""

    name = "video"

    def __init__(self, cam: str, endpoint: str, s: Settings, sink: Sink) -> None:
        super().__init__(sink)
        self.cam = cam
        self.endpoint = endpoint
        self.s = s
        self.tee: Optional[bool] = None
        self.rec: Optional[str] = None
        """The latest frame's `rec` header: the session the camera says it is
        also recording on its own disk (vision/cam-sender --record)."""
        self.rec_tm: Optional[int] = None
        self._rec_n: dict[str, int] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def key(self) -> str:
        return f"video:{self.cam}"

    def rec_count(self, session: str) -> int:
        """Frames received that the camera also kept locally for `session`."""
        return self._rec_n.get(session, 0)

    def _saw_rec(self, rec: Any, tm: int) -> None:
        if not isinstance(rec, str):
            self.rec = None
            return
        self.rec, self.rec_tm = rec, tm
        n = self._rec_n
        if rec not in n and len(n) >= REC_SESSIONS_KEPT:
            del n[next(iter(n))]                # the oldest session
        n[rec] = n.get(rec, 0) + 1

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=self.key)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def status(self) -> dict[str, Any]:
        st = super().status()
        dev = st.pop("devices").get(self.cam) or {"n": 0}
        st.update(dev)
        fresh = (self.rec_tm is not None
                 and time.perf_counter_ns() - self.rec_tm < REC_FRESH_NS)
        st.update({"endpoint": self.endpoint, "tee": self.tee,
                   "rec": self.rec if fresh else None})
        return st

    def _run(self) -> None:
        import zmq

        sock = _sub_socket(self.s.zmq_rcvhwm, b"")
        sock.connect(self.endpoint)
        log.info(f"[video] {self.cam}: SUB connect {self.endpoint}")
        try:
            while not self._stop.is_set():
                try:
                    parts = sock.recv_multipart()
                except zmq.Again:
                    continue
                tm, tw = stamp()
                self._handle(parts, tm, tw)
        except Exception as e:              # noqa: BLE001
            self.last_error = repr(e)
            log.error(f"[video] {self.cam}: stopped: {e!r}")
        finally:
            sock.close(0)

    def _handle(self, parts: list[bytes], tm: int, tw: float) -> None:
        try:
            own, up = split_tee(parts)
            self.tee = up is not None
            if not own:
                self.n_errors += 1
                return
            pre, jpeg = own[:-1], own[-1]
            hdr = frame_header(pre)
            self.counters.seen(self.cam, tm, len(jpeg), _seq(hdr.get("seq")))
            self._saw_rec(hdr.get("rec"), tm)
            self.sink(FrameMsg(self.cam, tm, tw, pre, jpeg, up))
        except Exception as e:              # noqa: BLE001
            self.n_errors += 1
            self.last_error = repr(e)


class AprilTagTap(Tap):
    """Every detector, on one SUB, prefix `apriltag` (records and meta). The
    record's `cam` is its device; `tick` is its gapless counter."""

    name = "apriltag"

    def __init__(self, endpoints: list[str], s: Settings, sink: Sink) -> None:
        super().__init__(sink)
        self.endpoints = endpoints
        self.s = s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="apriltag")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def status(self) -> dict[str, Any]:
        st = super().status()
        st["connect"] = self.endpoints
        return st

    def _run(self) -> None:
        import zmq

        sock = _sub_socket(self.s.zmq_rcvhwm, b"apriltag")
        for ep in self.endpoints:
            sock.connect(ep)
        log.info(f"[apriltag] SUB connect {', '.join(self.endpoints)}")
        try:
            while not self._stop.is_set():
                try:
                    parts = sock.recv_multipart()
                except zmq.Again:
                    continue
                tm, tw = stamp()
                self._handle(parts, tm, tw)
        except Exception as e:              # noqa: BLE001
            self.last_error = repr(e)
            log.error(f"[apriltag] stopped: {e!r}")
        finally:
            sock.close(0)

    def _handle(self, parts: list[bytes], tm: int, tw: float) -> None:
        try:
            if len(parts) < 2:
                self.n_errors += 1
                return
            topic, payload = parts[0], parts[-1]
            rec = json_obj(payload) or {}
            cam = rec.get("cam")
            if topic == b"apriltag":
                key = f"apriltag:{cam}"
                seq = _seq(rec.get("tick"))
            else:
                key = f"{topic.decode('utf-8', 'replace')}:{cam}"
                seq = None
            self.counters.seen(key, tm, len(payload), seq)
            self.sink(AprilMsg(tm, tw, topic, payload))
        except Exception as e:              # noqa: BLE001
            self.n_errors += 1
            self.last_error = repr(e)


def build(s: Settings, sink: Sink) -> tuple[Optional[MqttTap], list[Tap]]:
    """Every tap the settings ask for. The MQTT tap is returned separately
    because a session start asks it to resubscribe."""
    taps: list[Tap] = []
    mq: Optional[MqttTap] = None
    if s.mqtt_host and s.topics:
        mq = MqttTap(s, sink)
        taps.append(mq)
    for cam, ep in s.camera_map.items():
        taps.append(VideoTap(cam, ep, s, sink))
    if s.apriltag_endpoints:
        taps.append(AprilTagTap(s.apriltag_endpoints, s, sink))
    return mq, taps
