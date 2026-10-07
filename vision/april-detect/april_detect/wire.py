#!/usr/bin/env python3
"""
Both wire contracts, as plain functions so they can be tested without sockets.

INPUT -- frames from a camera. Full spec: SENDER.md.

    imageZMQ `send_jpg(msg, jpeg)` puts two frames on the wire:
        [ json({"msg": msg}),  jpeg_bytes ]
    `msg` is normally a camera name. imageZMQ JSON-encodes whatever it is given,
    so a sender passes a *dict* instead and the timing header rides along with
    no change to imageZMQ on either end:
        {"v": 1, "cam": "cam1", "seq": 1234, "t_ms": 5021.733, "t_send_ms": 5034.1}

    Also accepted, so every sender already in the repo works unmodified:
        msg is a plain string              -> camera name only, no timing
        msg is a string holding JSON       -> parsed as the dict above
        [topic, jpeg] (pi_mjpeg_pub.py)    -> topic is the camera name

INPUT is decoded leniently: an unparseable header costs the timing, not the
frame. Only a missing JPEG drops a frame.

OUTPUT -- detections to the funnel. Full spec: README.md, "Output contract".

    ZMQ PUB, two-frame multipart [topic, compact_json], the shape
    funnel/sources/zmq_pose.py documents for the vision feed:
        b"apriltag"        one record per processed frame
        b"apriltag_meta"   camera model and conventions, resent periodically so
                           a subscriber that joins late still gets it
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

TOPIC_TAGS = b"apriltag"
TOPIC_META = b"apriltag_meta"
SCHEMA = 1

JPEG_SOI = b"\xff\xd8"

HEADER_KEYS = ("v", "cam", "seq", "t_ms", "t_send_ms", "t", "w", "h")


@dataclass(slots=True)
class FrameHeader:
    """What the sender told us about a frame. Every field is optional."""

    cam: Optional[str] = None
    seq: Optional[int] = None
    t_ms: Optional[float] = None
    """Sender monotonic clock at capture, milliseconds."""
    t_send_ms: Optional[float] = None
    """Same clock, immediately before the send call."""
    t: Optional[float] = None
    """Unix epoch seconds at capture, only from an NTP-synced sender."""
    extra: dict[str, Any] = field(default_factory=dict)
    """Unrecognised header keys, kept for the record (not interpreted)."""
    ok: bool = True
    """False if a header was present but malformed."""


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and abs(f) != float("inf") else None


def parse_header(msg: Any) -> FrameHeader:
    """imageZMQ's `msg` -> FrameHeader."""
    if isinstance(msg, str):
        s = msg.strip()
        if s.startswith("{"):
            try:
                msg = json.loads(s)
            except ValueError:
                return FrameHeader(cam=msg, ok=False)
        else:
            return FrameHeader(cam=msg or None)
    if not isinstance(msg, dict):
        return FrameHeader(ok=False)

    h = FrameHeader()
    cam = msg.get("cam", msg.get("name"))
    h.cam = str(cam) if cam is not None else None
    seq = _num(msg.get("seq"))
    h.seq = int(seq) % (2 ** 32) if seq is not None else None
    h.t_ms = _num(msg.get("t_ms"))
    h.t_send_ms = _num(msg.get("t_send_ms"))
    t = _num(msg.get("t"))
    h.t = t if t is not None and t > 0 else None
    h.extra = {k: v for k, v in msg.items()
               if k not in HEADER_KEYS and k != "name"}
    return h


def decode_frame(parts: list[bytes]) -> Optional[tuple[FrameHeader, bytes]]:
    """One received multipart message -> (header, jpeg), or None if there is
    no JPEG in it."""
    if not parts:
        return None
    jpeg = parts[-1]
    if not jpeg.startswith(JPEG_SOI):
        # A topic glued onto the front of a single-part message.
        i = jpeg.find(JPEG_SOI, 0, 256)
        if i < 0:
            return None
        head, jpeg = jpeg[:i], jpeg[i:]
        if len(parts) == 1:
            return FrameHeader(cam=head.decode("utf-8", "replace").strip() or None), jpeg

    if len(parts) == 1:
        return FrameHeader(), jpeg

    first = parts[0]
    if first[:1] == b"{":
        try:
            md = json.loads(first)
        except ValueError:
            return FrameHeader(ok=False), jpeg
        if isinstance(md, dict) and "msg" in md:
            return parse_header(md["msg"]), jpeg        # imageZMQ
        return parse_header(md), jpeg                   # bare header dict
    # [topic, jpeg]: the topic is the only identity there is.
    return FrameHeader(cam=first.decode("utf-8", "replace") or None), jpeg


def encode(topic: bytes, obj: dict[str, Any]) -> list[bytes]:
    """Record -> the two frames sent. Compact separators, as pose_stream.py."""
    return [topic, json.dumps(obj, separators=(",", ":"),
                              allow_nan=False).encode()]


def r(v: Optional[float], nd: int) -> Optional[float]:
    """Round, passing None through. Keeps records small without losing
    meaningful precision (0.1 mm, 0.01 deg, 1 us)."""
    return None if v is None else round(float(v), nd)
