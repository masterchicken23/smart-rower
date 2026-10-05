#!/usr/bin/env python3
"""
MQTT ingest.

The wire contract is documented in full in this package's README; the short
version, for the seat distance sensor:

    topic    rower/<seat>/seat          seat is 1-based, bow = 1
    payload  {"seq": 412, "t_ms": 183450, "d_mm": 812, "v_mms": -350}
    QoS      0, not retained

`t_ms` is the sender's uptime in milliseconds *at the moment of measurement*.
Asking for the sampling instant rather than stamping on arrival is the point:
the existing sensor-esp-pipeline restamps in receiver.ino when the ESP-NOW
packet lands, so its timestamps carry radio latency and the receiver's own boot
epoch. ClockEstimator in store.py maps t_ms onto host time.

`seq` is a gapless per-sender counter, so dropped samples are counted rather
than silently interpolated over. Same role as `tick` in the pose records.

Decoding is split out as plain functions so the contract can be tested without
a broker -- see tests/test_mqtt_decode.py.
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

ENVELOPE = ("seq", "t_ms", "t", "dev")
"""Payload keys the funnel interprets itself. Everything else is a measurement
and is passed through to clients untouched."""

STATUS_STREAM = "status"


def parse_topic(topic: str) -> Optional[StreamKey]:
    """`rower/3/seat` -> ("rower", 3, "seat");  `boat/imu` -> ("boat", None, "imu").

    Returns None for anything else, including a rower segment that is not a
    positive integer. Unknown topics are counted, never fatal: a stray
    publisher on the boat's network must not be able to break ingest.
    """
    parts = [p for p in topic.strip().split("/") if p]
    if len(parts) == 3 and parts[0] == "rower":
        try:
            seat = int(parts[1])
        except ValueError:
            return None
        if seat < 1:
            return None                 # seats are 1-based, bow = 1
        return StreamKey("rower", seat, parts[2])
    if len(parts) == 2 and parts[0] == "boat":
        return StreamKey("boat", None, parts[1])
    return None


def decode_payload(raw: bytes) -> Optional[dict]:
    """One JSON object per publish. None if it is not one."""
    try:
        obj = json.loads(raw)
    except Exception:                   # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) else None


def to_sample(obj: dict, t_recv: float, wall: float,
              clock: Any = None) -> Sample:
    """Payload -> Sample, splitting the envelope from the measurements.

    Source timestamp, in order of preference:
      1. `t`, if the sender knows real epoch time (NTP-synced).
      2. `t_ms` mapped through the per-stream clock estimator.
      3. None -- the sender gave us nothing, so ages come from arrival only.
    """
    seq = obj.get("seq")
    seq = int(seq) % (2 ** 32) if isinstance(seq, (int, float)) else None

    t_src: Optional[float] = None
    t_epoch = obj.get("t")
    if isinstance(t_epoch, (int, float)) and t_epoch > 0:
        t_src = float(t_epoch)
    else:
        t_ms = obj.get("t_ms")
        if isinstance(t_ms, (int, float)) and clock is not None:
            t_src = clock.update(clock.unwrap(float(t_ms)), t_recv, wall)

    dev = obj.get("dev")
    values = {k: v for k, v in obj.items() if k not in ENVELOPE}
    return Sample(t_recv=t_recv, t_src=t_src, seq=seq, values=values,
                  dev=str(dev) if dev is not None else None)


class MqttSource(BaseSource):
    """Subscribes, decodes, and pushes. Reconnects forever.

    Everything from receipt to buffer insert is synchronous, so a sample never
    lands half-processed and the store needs no lock.
    """

    name = "mqtt"

    def __init__(self, store: StreamStore, settings: Settings,
                 recorder: Optional[Recorder] = None) -> None:
        super().__init__(store, recorder)
        self.settings = settings
        self.connected = False
        self.n_reconnects = 0
        self.last_error: Optional[str] = None

    def stats(self) -> dict[str, Any]:
        s = super().stats()
        s.update({
            "broker": f"{self.settings.mqtt_host}:{self.settings.mqtt_port}",
            "topics": self.settings.topics,
            "connected": self.connected,
            "reconnects": self.n_reconnects,
            "last_error": self.last_error,
        })
        return s

    async def run(self) -> None:
        # Deferred, matching the repo's habit of keeping heavy imports out of
        # module scope (see the ZMQ sites in network-video-yolo).
        import aiomqtt

        s = self.settings
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=s.mqtt_host,
                    port=s.mqtt_port,
                    identifier=s.mqtt_client_id,
                    keepalive=s.mqtt_keepalive_s,
                ) as client:
                    self.connected = True
                    self.last_error = None
                    for topic in s.topics:
                        await client.subscribe(topic, qos=0)
                    log.info(f"[mqtt] connected {s.mqtt_host}:{s.mqtt_port} "
                             f"subscribed {', '.join(s.topics)}")
                    async for msg in client.messages:
                        self._handle(str(msg.topic), bytes(msg.payload))
            except asyncio.CancelledError:
                self.connected = False
                log.info("[mqtt] stopped")
                raise
            except Exception as e:          # noqa: BLE001
                # Broker not up yet, or it restarted. Routine on a boat.
                self.connected = False
                self.last_error = repr(e)
                self.n_reconnects += 1
                if self.n_reconnects <= 3 or self.n_reconnects % 30 == 0:
                    log.warn(f"[mqtt] {e!r}; retrying in {s.mqtt_reconnect_s}s")
                await asyncio.sleep(s.mqtt_reconnect_s)

    def _handle(self, topic: str, payload: bytes) -> None:
        key = parse_topic(topic)
        if key is None:
            self.n_unknown += 1
            self.store.n_unknown_topic += 1
            if self.n_unknown <= 3:
                log.warn(f"[mqtt] ignoring unrecognised topic {topic!r}")
            return

        if key.stream == STATUS_STREAM and not payload.strip():
            # Zero-length retained publish: MQTT's way of retracting a
            # retained message. Honour it as "forget this seat" rather than
            # counting it as a malformed payload, so a sensor re-provisioned to
            # another seat does not leave the old one looking connected for
            # ever.
            self.n_msgs += 1
            self.store.clear_presence(key.ident)
            log.info(f"[mqtt] cleared retained status on {topic}")
            return

        obj = decode_payload(payload)
        if obj is None:
            self.n_bad += 1
            self.store.mark_bad(key)
            if self.n_bad <= 3:
                log.warn(f"[mqtt] undecodable payload on {topic}: "
                         f"{payload[:64]!r}")
            return

        if key.stream == STATUS_STREAM:
            self._handle_status(key, obj)
            return

        t_recv = time.monotonic()
        buf = self.store.buffer(key)
        sample = to_sample(obj, t_recv, time.time(), buf.clock)
        if not sample.values:
            # Envelope with no measurement in it. Counted, not buffered.
            self.n_bad += 1
            self.store.mark_bad(key)
            return
        self.emit(key, sample)

    def _handle_status(self, key: StreamKey, obj: dict) -> None:
        """Retained presence, published by the sensor on connect and set as its
        MQTT Last Will. Distinguishes "sensor is gone" from "sensor is
        connected but silent", which a sample age alone cannot."""
        self.n_msgs += 1
        if key.scope == "rower" and key.ident is not None:
            self.store.put_presence(key.ident, obj)
        else:
            self.store.boat_status = dict(obj, t_recv=time.monotonic())
