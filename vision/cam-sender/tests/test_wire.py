"""What the sender puts on the wire, decoded by the detector's own parser
(vision/april-detect/april_detect/wire.py) -- the contract in SENDER.md."""

from __future__ import annotations

import json
import socket
import time

import pytest
from april_detect import wire as detector_wire

from cam_sender import wire

JPEG = b"\xff\xd8\xff\xe0fakejpeg"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_packed_frame_decodes_with_full_timing():
    hdr = wire.make_header("cam1", 7, 1000.12345, w=1280, h=720, exposure_us=8000)
    hdr["t_send_ms"] = 1012.25
    h, jpeg = detector_wire.decode_frame(wire.pack(hdr, JPEG))
    assert jpeg == JPEG
    assert h.ok
    assert (h.cam, h.seq, h.t_ms, h.t_send_ms, h.t) == ("cam1", 7, 1000.123, 1012.25, None)
    assert h.extra == {"exposure_us": 8000}            # v, w, h are known keys


def test_seq_wraps_at_32_bits():
    assert wire.make_header("c", 2 ** 32 + 5, 0.0)["seq"] == 5


def test_bytes_match_imagezmq_send_jpg():
    """Same bytes as imageZMQ's send_jpg, so imageZMQ receivers work too."""
    imagezmq = pytest.importorskip("imagezmq")
    import zmq

    ctx = imagezmq.SerializingContext()
    a, b = ctx.socket(zmq.PAIR), ctx.socket(zmq.PAIR)
    a.bind("inproc://cam-sender-compat")
    b.connect("inproc://cam-sender-compat")
    try:
        hdr = wire.make_header("cam1", 3, 5.5, w=640, h=480)
        hdr["t_send_ms"] = 6.0
        a.send_jpg(hdr, JPEG)
        assert b.recv_multipart() == wire.pack(hdr, JPEG)
    finally:
        a.close(0)
        b.close(0)


def test_publisher_over_a_real_socket():
    import zmq

    addr = f"tcp://127.0.0.1:{free_port()}"
    pub = wire.Publisher(addr, sndhwm=2)
    sub = zmq.Context.instance().socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.setsockopt(zmq.RCVTIMEO, 100)
    sub.connect(addr)
    try:
        now_ms = lambda: time.perf_counter() * 1000.0  # noqa: E731
        got = None
        deadline = time.monotonic() + 5.0
        seq = 0
        while got is None and time.monotonic() < deadline:  # PUB slow joiner
            t_ms = now_ms()
            assert pub.send(wire.make_header("cam9", seq, t_ms), JPEG, now_ms)
            seq += 1
            try:
                got = sub.recv_multipart()
            except zmq.Again:
                pass
        assert got is not None, "no frame received"
        h, jpeg = detector_wire.decode_frame(got)
        assert jpeg == JPEG
        assert h.cam == "cam9"
        assert h.t_send_ms >= h.t_ms
        assert json.loads(got[0])["msg"]["v"] == 1
    finally:
        sub.close(0)
        pub.close()
