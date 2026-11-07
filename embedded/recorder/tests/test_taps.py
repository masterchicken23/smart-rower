"""The receivers: what each one makes of a message, its counters, and -- for
video, over a real loopback socket -- that stamps are taken in order."""

from __future__ import annotations

import json
import socket
import time
from types import SimpleNamespace

from recorder.format import AprilMsg, FrameMsg, MqttMsg
from recorder.taps import AprilTagTap, MqttTap, VideoTap

from .helpers import JPEG, april, cam_pre, settings


def test_video_tap_accepts_tee_and_direct_messages(tmp_path):
    got: list = []
    tap = VideoTap("cam1", "tcp://unused:1", settings(tmp_path), got.append)
    stamp = json.dumps({"t_mono": 3.25, "t_wall": 1700.5}).encode()
    tap._handle([b"tee1", stamp, *cam_pre(1, 1.0, 2.0), JPEG], 10, 1.0)
    tap._handle([*cam_pre(2, 3.0, 4.0), JPEG], 20, 2.0)
    tap._handle([*cam_pre(5, 5.0, 6.0), JPEG], 30, 3.0)       # 3 and 4 lost
    assert [type(x) for x in got] == [FrameMsg] * 3
    assert got[0].up == (3_250_000_000, 1700.5) and got[1].up is None
    assert got[0].pre == cam_pre(1, 1.0, 2.0) and got[0].jpeg == JPEG
    st = tap.status()
    assert st["n"] == 3 and st["gaps"] == 2


def test_apriltag_tap_counts_ticks_per_camera(tmp_path):
    got: list = []
    tap = AprilTagTap(["tcp://unused:1"], settings(tmp_path), got.append)
    for tick in (1, 2, 4):
        tap._handle([b"apriltag", april(tick, tick, 1.0)], tick, 1.0)
    tap._handle([b"apriltag_meta", b'{"cam":"cam1"}'], 9, 1.0)
    assert all(isinstance(x, AprilMsg) for x in got) and len(got) == 4
    dev = tap.counters.view()
    assert dev["apriltag:cam1"]["gaps"] == 1
    assert dev["apriltag_meta:cam1"]["n"] == 1


def test_mqtt_tap_keeps_payload_and_flags(tmp_path):
    got: list = []
    tap = MqttTap(settings(tmp_path, mqtt_host="x"), got.append)
    for seq in (1, 2, 3, 0):                # 0: the sensor rebooted
        tap._on_message(None, None, SimpleNamespace(
            topic="rower/1/seat", qos=0, retain=False,
            payload=b'{"seq":%d,"t_ms":1}' % seq))
    tap._on_message(None, None, SimpleNamespace(
        topic="rower/1/status", qos=1, retain=True, payload=b""))
    assert all(isinstance(x, MqttMsg) for x in got)
    assert got[-1].payload == b"" and got[-1].retain and got[-1].qos == 1
    dev = tap.counters.view()["rower/1/seat"]
    assert dev["gaps"] == 0 and dev["restarts"] == 1
    tms = [x.tm for x in got]
    assert tms == sorted(tms)


def test_video_tap_over_a_real_socket(tmp_path):
    import zmq

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    pub = zmq.Context.instance().socket(zmq.PUB)
    pub.setsockopt(zmq.LINGER, 0)
    pub.bind(f"tcp://127.0.0.1:{port}")
    got: list = []
    tap = VideoTap("cam1", f"tcp://127.0.0.1:{port}", settings(tmp_path), got.append)
    tap.start()
    try:
        deadline = time.monotonic() + 5.0
        seq = 0
        while len(got) < 20 and time.monotonic() < deadline:
            pub.send_multipart([*cam_pre(seq, 1.0, 2.0), JPEG])
            seq += 1
            time.sleep(0.01)
    finally:
        tap.stop()
        pub.close(0)
    assert len(got) >= 20
    tms = [x.tm for x in got]
    assert tms == sorted(tms)
    # Frames sent before the SUB joined are never seen (and never counted);
    # once connected, loopback loses nothing.
    assert tap.status()["gaps"] == 0
