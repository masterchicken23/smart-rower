#!/usr/bin/env python3
"""
AprilTag ingest and its REST surface.

The record below is the example from vision/april-detect/README.md, "Output
contract", so a change on the detector side that breaks the funnel shows up
here. Most tests drive the source's `_handle` directly with the two frames the
detector puts on the wire; the last one goes through a real ZMQ socket.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from funnel.api.app import create_app
from funnel.config import Settings, parse_camera_seats
from funnel.models import Sample, StreamKey
from funnel.sources import zmq_apriltag as za

RECORD = {
    "type": "apriltag", "v": 1, "cam": "cam1", "tick": 1842, "seq": 18342,
    "skipped": 0, "t": 1760000000.0005, "clock": "est", "w": 1280, "h": 720,
    "ts": {"cap": 1760000000.0005, "send": 1760000000.0093,
           "recv": 1760000000.0096, "start": 1760000000.0098,
           "pub": 1760000000.0251},
    "ms": {"enc": 8.8, "net": 0.3, "queue": 0.2, "decode": 2.5,
           "undistort": 4.4, "detect": 7.7, "pose": 0.5, "proc": 15.3,
           "total": 15.5, "e2e": 24.6},
    "n": 1,
    "tags": [{
        "id": 0, "fam": "tag36h11", "size_m": 0.1,
        "pos_m": [0.1117, 0.0201, 1.0661],
        "quat": [0.98821, 0.09373, 0.11874, 0.0237],
        "euler_deg": [11.31, 13.31, 4.07],
        "dist_m": 1.0721,
        "center_px": [734.29, 376.17],
        "corners_px": [[778.56, 337.55], [693.8, 332.24],
                       [690.78, 413.92], [774.02, 420.98]],
        "hamming": 0, "margin": 119.03, "err_px": 0.02, "ambiguity": 0.03,
    }],
}

META = {"type": "apriltag_meta", "v": 1, "cam": "cam1", "detector": "cpu",
        "frame": [1280, 720], "K_rect": [900, 0, 639.5, 0, 900, 359.5, 0, 0, 1]}


def wire(topic: bytes, obj) -> list[bytes]:
    """What april_detect.wire.encode puts on the socket."""
    return [topic, json.dumps(obj, separators=(",", ":")).encode()]


def rec(**kw) -> dict:
    return {**RECORD, **kw}


def make_app(**kw):
    base = {"mqtt_host": "127.0.0.1", "max_age_ms": 1000.0,
            "record_enabled": False, "apriltag_enabled": True,
            "apriltag_connect": "tcp://127.0.0.1:1",
            "apriltag_cameras": "cam1:1,cam2:4"}
    base.update(kw)
    app = create_app(Settings(**base))
    parts = app.state.parts
    src = next(s for s in parts["sources"]
               if isinstance(s, za.ZmqAprilTagSource))
    return app, parts, src


async def get(app, path):
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://funnel.test") as c:
        return await c.get(path)


SEAT1 = StreamKey("rower", 1, "apriltag")
SEAT4 = StreamKey("rower", 4, "apriltag")


# ---- camera -> seat map ------------------------------------------------ #

def test_camera_seat_map_parses():
    assert parse_camera_seats("") == {}
    assert parse_camera_seats(" cam1:1, cam_2:8 ,") == {"cam1": 1, "cam_2": 8}


@pytest.mark.parametrize("spec", [
    "cam1", "cam1:x", "cam1:0", ":3", "a/b:1",
    "cam1:1,cam1:2",                # one camera, two seats
    "cam1:1,cam2:1",                # two cameras, one seat
])
def test_bad_camera_seat_map_stops_startup(spec):
    with pytest.raises(ValueError):
        Settings(apriltag_cameras=spec)


# ---- decoding ---------------------------------------------------------- #

def test_sample_maps_envelope_and_keeps_record_whole():
    s = za.to_sample(RECORD, t_recv=5.0)
    assert s.seq == 1842                    # tick, not the camera's seq
    assert s.t_src == RECORD["t"]
    assert s.dev == "cam1"
    assert s.values == RECORD               # raw: nothing stripped


def test_decode_rejects_wrong_shapes():
    assert za.decode_message([b"apriltag"]) is None
    assert za.decode_message([b"apriltag", b"not json"]) is None
    assert za.decode_message([b"apriltag", b"[1,2]"]) is None
    assert za.decode_message(wire(za.TOPIC_TAGS, RECORD)) == (b"apriltag", RECORD)


# ---- source ------------------------------------------------------------ #

def test_record_lands_under_the_cameras_seat():
    _, parts, src = make_app()
    src._handle(wire(za.TOPIC_TAGS, rec()))
    src._handle(wire(za.TOPIC_TAGS, rec(cam="cam2")))
    store = parts["store"]
    assert store.get(SEAT1).latest.values["tags"][0]["id"] == 0
    assert store.get(SEAT4).latest.values["cam"] == "cam2"
    assert store.seats() == [1, 4]


def test_unmapped_camera_is_counted_not_guessed():
    _, parts, src = make_app()
    for _ in range(3):
        src._handle(wire(za.TOPIC_TAGS, rec(cam="cam3")))
    src._handle(wire(za.TOPIC_META, {**META, "cam": "cam3"}))
    assert not parts["store"].buffers
    assert not parts["store"].apriltag_meta
    assert src.stats()["unmapped"] == {"cam3": 3}
    assert src.stats()["cameras"] == {"cam1": "rower/1/apriltag",
                                      "cam2": "rower/4/apriltag"}


def test_tick_gaps_are_counted_and_restart_is_not():
    _, parts, src = make_app()
    for t in (1, 2, 5):
        src._handle(wire(za.TOPIC_TAGS, rec(tick=t)))
    buf = parts["store"].get(SEAT1)
    assert buf.n_gap == 2
    src._handle(wire(za.TOPIC_TAGS, rec(tick=1)))   # container restarted
    assert buf.n_gap == 2


def test_meta_is_kept_by_seat_beside_the_buffers():
    _, parts, src = make_app()
    src._handle(wire(za.TOPIC_META, META))
    src._handle(wire(za.TOPIC_META, {**META, "cam": None}))  # before 1st frame
    store = parts["store"]
    assert set(store.apriltag_meta) == {1}
    assert not store.buffers                 # never a stream, never stale
    assert src.n_bad == 0


def test_bad_messages_are_counted_not_fatal():
    _, parts, src = make_app()
    src._handle([b"apriltag", b"{"])
    src._handle(wire(za.TOPIC_TAGS, rec(cam=None)))
    src._handle(wire(za.TOPIC_TAGS, rec(cam="a/b")))
    src._handle(wire(za.TOPIC_TAGS, rec(tags="nope")))
    src._handle(wire(b"apriltag_other", RECORD))
    assert src.n_bad == 4
    assert src.n_unknown == 1
    assert src.n_msgs == 0


# ---- REST -------------------------------------------------------------- #

async def test_rower_apriltag_serves_the_raw_record():
    app, parts, src = make_app()
    src._handle(wire(za.TOPIC_TAGS, rec(cam="cam2")))
    parts["loop"].tick_once()

    r = await get(app, "/rower/4/apriltag")
    assert r.status_code == 200
    body = r.json()
    assert body["seat"] == 4
    assert body["cam"] == "cam2"
    assert body["tick"] == 1842
    assert body["t"] == pytest.approx(RECORD["t"])
    assert body["stale"] is False
    assert body["record"] == rec(cam="cam2")
    assert r.headers["X-Funnel-Status"] == "ok"

    # The seat's listing and the snapshot carry it too.
    assert "apriltag" in (await get(app, "/rower/4")).json()["streams"]
    snap = (await get(app, "/snapshot")).json()
    assert snap["rowers"]["4"]["apriltag"]["values"]["tags"]


async def test_stale_camera_is_503():
    app, parts, src = make_app()
    src._handle(wire(za.TOPIC_TAGS, rec()))
    parts["store"].get(SEAT1).latest.t_recv -= 5.0      # camera went dark
    parts["loop"].tick_once()
    r = await get(app, "/rower/1/apriltag")
    assert r.status_code == 503
    assert r.headers["X-Funnel-Status"] == "stale"


async def test_missing_seat_or_camera_is_404():
    app, parts, _ = make_app()
    parts["store"].push(StreamKey("rower", 3, "seat"), Sample(
        t_recv=time.monotonic(), t_src=None, seq=1, values={"d_mm": 800}))
    parts["loop"].tick_once()
    assert (await get(app, "/rower/9/apriltag")).status_code == 404
    r = await get(app, "/rower/3/apriltag")         # seat known, no camera
    assert r.status_code == 404
    assert "FUNNEL_APRILTAG_CAMERAS" in r.json()["detail"]
    assert (await get(app, "/rower/3/apriltag/meta")).status_code == 404


async def test_meta_and_raw_window():
    app, parts, src = make_app()
    src._handle(wire(za.TOPIC_META, META))
    for t in (1, 2, 3):
        src._handle(wire(za.TOPIC_TAGS, rec(tick=t)))
    parts["loop"].tick_once()

    r = await get(app, "/rower/1/apriltag/meta")
    assert r.status_code == 200
    assert r.json()["seat"] == 1
    assert r.json()["meta"] == META

    r = await get(app, "/rower/1/raw?stream=apriltag&window_s=5")
    assert r.status_code == 200
    assert r.json()["path"] == "rower/1/apriltag"
    assert [x["seq"] for x in r.json()["samples"]] == [1, 2, 3]


async def test_disabled_by_default():
    app = create_app(Settings(mqtt_host="127.0.0.1", record_enabled=False))
    assert not any(isinstance(s, za.ZmqAprilTagSource)
                   for s in app.state.parts["sources"])


# ---- over a real socket ------------------------------------------------ #

def test_ingest_over_zmq_from_a_binding_publisher():
    """Detector topology: it binds a PUB, the funnel connects a SUB.

    Run on a selector loop explicitly: pyzmq's asyncio sockets need add_reader,
    which Windows' default Proactor loop lacks. `python -m funnel` makes the
    same choice."""
    zmq = pytest.importorskip("zmq")
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(_ingest_over_zmq(zmq))


async def _ingest_over_zmq(zmq):
    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB)
    pub.setsockopt(zmq.LINGER, 0)
    port = pub.bind_to_random_port("tcp://127.0.0.1")
    try:
        _, parts, src = make_app(apriltag_connect=f"tcp://127.0.0.1:{port}")
        task = asyncio.create_task(src.run())
        buf_key = StreamKey("rower", 1, "apriltag")
        deadline = time.monotonic() + 5.0
        tick = 0
        # PUB drops until the subscription propagates; keep publishing.
        while parts["store"].get(buf_key) is None:
            assert not task.done(), task.exception()
            assert time.monotonic() < deadline, "nothing received over ZMQ"
            tick += 1
            pub.send_multipart(wire(za.TOPIC_TAGS, rec(tick=tick)))
            pub.send_multipart(wire(za.TOPIC_META, META))
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert parts["store"].get(buf_key).latest.values["cam"] == "cam1"
        assert src.n_errors == 0
    finally:
        pub.close()
        ctx.term()
