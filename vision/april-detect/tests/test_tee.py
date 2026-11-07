"""The raw frame tee (APRIL_TEE_ADDRESS): every received message passed on
byte for byte, stamped, for embedded/recorder -- and never at the expense of
detection."""

from __future__ import annotations

import json
import socket
import threading
import time

import zmq

from april_detect import wire
from april_detect.config import Settings
from april_detect.pipeline import LatestSlot, Receiver

JPEG = b"\xff\xd8" + b"x" * 500 + b"\xff\xd9"
HDR = json.dumps({"msg": {"v": 1, "cam": "cam1", "seq": 0, "t_ms": 1.0,
                          "t_send_ms": 2.0}}).encode()


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def settings(**kw) -> Settings:
    base = {"heartbeat_path": "", "recv_timeout_ms": 50}
    base.update(kw)
    return Settings(_env_file=None, **base)


def test_tee_message_shape():
    parts = wire.tee([HDR, JPEG], 12.000000123, 1760000000.25)
    assert parts[0] == wire.TEE_MARK
    assert json.loads(parts[1]) == {"t_mono": 12.000000123, "t_wall": 1760000000.25}
    assert parts[2:] == [HDR, JPEG]


def _run_receiver(s: Settings, send_to: zmq.Socket, tee_ep: str | None):
    ctx = zmq.Context.instance()
    sub = None
    if tee_ep:
        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        sub.setsockopt(zmq.RCVTIMEO, 100)
        sub.connect(tee_ep)
    slot, stop = LatestSlot(), threading.Event()
    recv = Receiver(s, slot, stop)
    recv.start()
    got: list = []
    deadline = time.monotonic() + 5.0
    try:
        while time.monotonic() < deadline and (len(got) < 3 if sub else recv.n_recv < 3):
            send_to.send_multipart([HDR, JPEG])
            if sub is not None:
                try:
                    got.append(sub.recv_multipart())
                except zmq.Again:
                    pass
            else:
                time.sleep(0.05)
    finally:
        stop.set()
        recv.join(2.0)
        if sub is not None:
            sub.close(0)
    return recv, slot, got


def test_receiver_tees_every_frame_unchanged_and_still_feeds_the_slot():
    src_port, tee_port = _port(), _port()
    pub = zmq.Context.instance().socket(zmq.PUB)
    pub.setsockopt(zmq.LINGER, 0)
    pub.bind(f"tcp://127.0.0.1:{src_port}")
    try:
        s = settings(source=f"tcp://127.0.0.1:{src_port}",
                     tee_address=f"tcp://127.0.0.1:{tee_port}")
        recv, slot, got = _run_receiver(s, pub, f"tcp://127.0.0.1:{tee_port}")
    finally:
        pub.close(0)
    assert len(got) >= 3
    for parts in got:
        assert parts[0] == b"tee1" and parts[2:] == [HDR, JPEG]
        st = json.loads(parts[1])
        assert st["t_mono"] > 0 and st["t_wall"] > 1e9
    stamps = [json.loads(p[1])["t_mono"] for p in got]
    assert stamps == sorted(stamps)
    assert recv.n_tee >= 3
    item = slot.take(0.1)                       # detection got its frames too
    assert item is not None and item[0] == [HDR, JPEG]


def test_a_tee_that_cannot_bind_does_not_stop_detection():
    src_port, taken = _port(), _port()
    ctx = zmq.Context.instance()
    blocker = ctx.socket(zmq.PUB)
    blocker.bind(f"tcp://127.0.0.1:{taken}")
    pub = ctx.socket(zmq.PUB)
    pub.setsockopt(zmq.LINGER, 0)
    pub.bind(f"tcp://127.0.0.1:{src_port}")
    try:
        s = settings(source=f"tcp://127.0.0.1:{src_port}",
                     tee_address=f"tcp://127.0.0.1:{taken}")
        recv, slot, _ = _run_receiver(s, pub, None)
    finally:
        pub.close(0)
        blocker.close(0)
    assert recv.n_recv >= 3 and recv.n_tee == 0
    assert slot.take(0.1) is not None
