"""Camera sync: the state the cameras' local recorders follow (camsync.py),
over a real loopback socket, and the confirmation that comes back in their
frames' headers (taps.py)."""

from __future__ import annotations

import json
import socket
import time

import zmq

from recorder.api import create_app
from recorder.camsync import CamSync
from recorder.format import read_meta
from recorder.session import SessionManager
from recorder.taps import VideoTap

from .helpers import JPEG, settings


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pre(seq: int, rec=None) -> list[bytes]:
    h = {"v": 1, "cam": "cam1", "seq": seq, "t_ms": float(seq),
         "t_send_ms": seq + 1.0, "w": 640, "h": 360}
    if rec is not None:
        h["rec"] = rec
    return [json.dumps({"msg": h}).encode()]


class Camera:
    """What vision/cam-sender's control thread does: a SUB, connected."""

    def __init__(self, endpoint: str) -> None:
        self.sock = zmq.Context.instance().socket(zmq.SUB)
        self.sock.setsockopt(zmq.SUBSCRIBE, b"")
        self.sock.setsockopt(zmq.RCVTIMEO, 100)
        self.sock.setsockopt(zmq.LINGER, 0)
        self.sock.connect(endpoint)
        self.heard: list[dict] = []

    def until(self, cond, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = json.loads(self.sock.recv())
            except zmq.Again:
                continue
            self.heard.append(msg)
            if cond(msg):
                return msg
        return None

    def close(self) -> None:
        self.sock.close(0)


def test_cameras_hear_idle_then_the_session_then_idle(tmp_path):
    ep = f"tcp://127.0.0.1:{_free_port()}"
    cs = CamSync(ep, enabled=True, interval_s=0.05)
    m = SessionManager(settings(tmp_path))
    m.attach(None, [], cs)
    cs.start()
    cam = Camera(ep)
    try:
        msg = cam.until(lambda _m: True)
        assert msg is not None, "nothing published"
        assert (msg["v"], msg["type"], msg["sync"], msg["session"]) == (1, "rec", True, None)
        assert isinstance(msg["tm"], int) and msg["tw"] > 0

        info = m.start("drill")
        msg = cam.until(lambda x: x["session"] is not None)
        assert msg["session"] == info["session"] and msg["state"] == 1
        st = m.status()["cam_sync"]
        assert st["listening"] and st["session"] == info["session"]
        # Repeated, so a camera that joins late is put right.
        n = len(cam.heard)
        assert cam.until(lambda _m: len(cam.heard) >= n + 3)
        assert all(x["session"] == info["session"] for x in cam.heard[n:])

        m.stop()
        msg = cam.until(lambda x: x["session"] is None)
        assert msg["state"] == 2
    finally:
        cs.close()
        cam.close()
    meta = read_meta(tmp_path / info["session"])
    assert meta["sources"]["cam_sync"]["enabled"] is True
    assert meta["summary"]["cam_local"] == {}


def test_sync_off_tells_the_cameras_nothing_is_recording(tmp_path):
    ep = f"tcp://127.0.0.1:{_free_port()}"
    cs = CamSync(ep, enabled=False, interval_s=0.05)
    m = SessionManager(settings(tmp_path))
    m.attach(None, [], cs)
    cs.start()
    cam = Camera(ep)
    try:
        assert cam.until(lambda _m: True) is not None
        m.start()
        cam.until(lambda _m: False, timeout=0.5)            # listen a while
        assert cam.heard and all(x["session"] is None and x["sync"] is False
                                 for x in cam.heard)
        assert m.status()["cam_sync"]["session"] is None
        summary = m.stop()
    finally:
        cs.close()
        cam.close()
    assert summary["cam_local"] is None


def test_shutdown_says_idle_last(tmp_path):
    ep = f"tcp://127.0.0.1:{_free_port()}"
    cs = CamSync(ep, interval_s=0.05)
    cs.start()
    cam = Camera(ep)
    try:
        cs.set("S")
        assert cam.until(lambda x: x["session"] == "S")
        cs.close()
        assert cam.until(lambda x: x["session"] is None)
    finally:
        cam.close()


def test_empty_bind_opens_no_socket(tmp_path):
    app = create_app(settings(tmp_path, cam_control_bind=""))
    assert app.state.manager.camsync is None
    cs = CamSync("")
    cs.start()
    cs.set("S")                                 # harmless with no socket
    assert cs.status()["listening"] is False
    cs.close()


def test_a_bind_that_fails_does_not_stop_the_recorder(tmp_path):
    blocker = zmq.Context.instance().socket(zmq.PUB)
    port = blocker.bind_to_random_port("tcp://127.0.0.1")
    try:
        cs = CamSync(f"tcp://127.0.0.1:{port}")
        cs.start()                              # logs, does not raise
        assert cs.status()["listening"] is False
        assert "bind" in cs.status()["last_error"]
        cs.close()
    finally:
        blocker.close(0)


def test_frames_confirm_what_the_camera_keeps(tmp_path):
    m = SessionManager(settings(tmp_path))
    tap = VideoTap("cam1", "tcp://unused:1", settings(tmp_path), m.offer)
    m.attach(None, [tap], CamSync("", enabled=True))
    name = m.start()["session"]

    def frame(seq: int, rec=None) -> None:
        tap._handle([*_pre(seq, rec), JPEG], time.perf_counter_ns(), time.time())

    for i in range(3):
        frame(i, name)
    assert m.status()["sources"]["video"]["cam1"]["rec"] == name
    frame(3)                                    # one the camera did not keep
    assert tap.status()["rec"] is None
    frame(4, name)
    tap.rec_tm = time.perf_counter_ns() - 3_000_000_000
    assert tap.status()["rec"] is None          # stale: shown as null
    summary = m.stop()
    assert summary["cam_local"] == {"cam1": 4}
    assert summary["written"] == {"cam1": 5}
