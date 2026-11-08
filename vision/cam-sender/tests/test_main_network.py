"""The whole sender, end to end on loopback, through a bad network: one
subscriber that stalls (connected, never reading, as a dead Wi-Fi link
behaves), and a recorder whose control messages stop mid-session. The frame
loop must keep its rate, the local recording must keep every frame, and the
session must outlive the silence."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
from april_detect import wire as detector_wire

from cam_sender.__main__ import main

zmq = pytest.importorskip("zmq")
fmt = pytest.importorskip("recorder.format")

FPS = 30.0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_frames_keep_flowing_and_recording_through_a_stalled_link(tmp_path):
    try:
        import simplejpeg  # noqa: F401
    except ImportError:
        pytest.importorskip("cv2")
    ctx = zmq.Context.instance()
    control = ctx.socket(zmq.PUB)
    control.setsockopt(zmq.LINGER, 0)
    cport = control.bind_to_random_port("tcp://127.0.0.1")
    fport = free_port()
    stop = threading.Event()
    argv = ["--backend", "test", "--name", "cam1",
            "--bind", f"tcp://127.0.0.1:{fport}",
            "--width", "320", "--height", "240", "--fps", str(FPS),
            "--record", "--record-dir", str(tmp_path),
            "--rec-control", f"tcp://127.0.0.1:{cport}",
            "--rec-min-free-mb", "0", "--stats-interval", "1", "--heartbeat", ""]
    rc: list[int] = []
    sender = threading.Thread(target=lambda: rc.append(main(argv, stop)))
    sender.start()

    # The stalled link: tiny buffers, never read.
    stalled = ctx.socket(zmq.SUB)
    stalled.setsockopt(zmq.RCVHWM, 1)
    stalled.setsockopt(zmq.RCVBUF, 4096)
    stalled.setsockopt(zmq.SUBSCRIBE, b"")
    stalled.setsockopt(zmq.LINGER, 0)
    stalled.connect(f"tcp://127.0.0.1:{fport}")
    # A healthy one, to see the `rec` confirmation in the header.
    live = ctx.socket(zmq.SUB)
    live.setsockopt(zmq.SUBSCRIBE, b"")
    live.setsockopt(zmq.RCVTIMEO, 200)
    live.setsockopt(zmq.LINGER, 0)
    live.connect(f"tcp://127.0.0.1:{fport}")

    try:
        # The recorder says "recording S" for 2 s, then goes silent.
        t_end = time.monotonic() + 2.0
        while time.monotonic() < t_end:
            control.send(json.dumps({"v": 1, "type": "rec", "sync": True,
                                     "session": "S", "state": 1,
                                     "tm": time.perf_counter_ns(),
                                     "tw": time.time()}).encode())
            time.sleep(0.05)
        control.close(0)
        rec_seen = None
        t_end = time.monotonic() + 2.0
        while time.monotonic() < t_end:
            try:
                got = live.recv_multipart()
            except zmq.Again:
                continue
            h, _ = detector_wire.decode_frame(got)
            rec_seen = h.extra.get("rec", rec_seen)
    finally:
        stop.set()
        sender.join(15.0)
        stalled.close(0)
        live.close(0)
    assert not sender.is_alive() and rc == [0]
    assert rec_seen == "S"

    with fmt.Session(tmp_path / "S") as sess:
        evs = list(sess.frames("cam1"))
        seqs = [fmt.frame_header(e.pre)["seq"] for e in evs]
        m = sess.meta
    # Every frame the loop produced while recording is on disk, in order.
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    # The session outlived 2 s of silence and ended only with the sender.
    assert m["stop_reason"] == "shutdown" and m["complete"] is True
    seg = m["segments"][0]
    assert seg["dropped"] == 0 and seg["write_errors"] == 0
    # And the loop kept its rate: no stalls behind the dead subscriber.
    assert seg["duration_s"] > 3.0
    assert len(seqs) >= 0.8 * FPS * seg["duration_s"], (len(seqs), seg["duration_s"])
