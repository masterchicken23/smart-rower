#!/usr/bin/env python3
"""
The output contract: vision/april-detect/SENDER.md.

    frame 0:  JSON  {"msg": <header>}
    frame 1:  JPEG bytes

which is byte-for-byte what imageZMQ's `ImageSender.send_jpg(header, jpeg)`
puts on the wire, so any imageZMQ receiver (pose_stream.py) works too. It is
written with plain pyzmq instead: imagezmq would add a dependency, and on a
Pi Zero numpy alone takes seconds to import.

    {"v": 1, "cam": "cam1", "seq": 1234, "t_ms": 5021733.104,
     "t_send_ms": 5021747.882, "w": 1280, "h": 720}
"""

from __future__ import annotations

import json
from typing import Any, Callable

VERSION = 1


def make_header(cam: str, seq: int, t_ms: float, **extra: Any) -> dict[str, Any]:
    """Everything except `t_send_ms`, which Publisher.send stamps last."""
    h: dict[str, Any] = {"v": VERSION, "cam": cam, "seq": seq % (2 ** 32),
                         "t_ms": round(t_ms, 3)}
    h.update(extra)
    return h


def pack(header: dict[str, Any], jpeg: bytes) -> list[bytes]:
    """Header and JPEG -> the two frames. json.dumps with default separators,
    as pyzmq's send_json (which imageZMQ uses) produces."""
    return [json.dumps({"msg": header}).encode("utf8"), jpeg]


def stamp_and_pack(header: dict[str, Any], jpeg: bytes,
                   now_ms: Callable[[], float]) -> list[bytes]:
    """Stamp `t_send_ms` and pack. Call it immediately before
    Publisher.send_parts, so the stamp stays the last thing before the send.
    Kept apart from the send so the local recording stores the very bytes
    that went out, whether or not the send succeeded."""
    header["t_send_ms"] = round(now_ms(), 3)
    return pack(header, jpeg)


class Publisher:
    """The PUB socket. The sender binds; the detector connects."""

    def __init__(self, bind: str, sndhwm: int = 2) -> None:
        import zmq

        self._zmq = zmq
        self.sock = zmq.Context.instance().socket(zmq.PUB)
        # PUB drops at the high-water mark instead of blocking, so a slow or
        # absent detector costs frames, never latency. Set before bind so it
        # applies to every subscriber.
        self.sock.setsockopt(zmq.SNDHWM, sndhwm)
        self.sock.setsockopt(zmq.LINGER, 0)
        self.sock.bind(bind)

    def send_parts(self, parts: list[bytes]) -> bool:
        """Send packed frames. Never waits on the network: NOBLOCK, and a PUB
        drops at the high-water mark anyway. libzmq does the TCP writes on
        its own I/O thread, so a stalled Wi-Fi link fills the pipe and costs
        frames (False here), never time on the caller's thread. False if the
        send failed; the caller still spends the seq, so the gap is visible
        downstream."""
        try:
            self.sock.send_multipart(parts, flags=self._zmq.NOBLOCK, copy=False)
        except self._zmq.ZMQError:
            return False
        return True

    def send(self, header: dict[str, Any], jpeg: bytes,
             now_ms: Callable[[], float]) -> bool:
        """Stamp `t_send_ms` and send (stamp_and_pack + send_parts)."""
        return self.send_parts(stamp_and_pack(header, jpeg, now_ms))

    def close(self) -> None:
        self.sock.close(0)
