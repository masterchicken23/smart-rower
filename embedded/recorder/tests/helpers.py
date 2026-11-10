"""Shared builders for the tests: settings, and synthetic sessions written
through the real SessionWriter, so the reader is tested against the writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from recorder.config import Settings
from recorder.format import AprilMsg, FrameMsg, MqttMsg, SessionWriter, write_meta

SEC = 1_000_000_000
JPEG = b"\xff\xd8" + b"jpeg-bytes" * 20 + b"\xff\xd9"


def settings(tmp: Path, **kw) -> Settings:
    base = {"dir": str(tmp), "cameras": "", "apriltag_connect": "",
            "mqtt_host": "", "stats_interval_s": 0.0, "disk_check_s": 0.0,
            "min_free_gb": 0.0, "fsync_s": 0.0}
    base.update(kw)
    return Settings(_env_file=None, **base)


def seat(seq: int, t_ms: int, d_mm: int = 500) -> bytes:
    return json.dumps({"seq": seq, "t_ms": t_ms, "d_mm": d_mm,
                       "dev": "ab12cd"}).encode()


def cam_pre(seq: int, t_ms: float, t_send_ms: float, cam: str = "cam1",
            t: Optional[float] = None) -> list[bytes]:
    h = {"v": 1, "cam": cam, "seq": seq, "t_ms": t_ms, "t_send_ms": t_send_ms,
         "w": 1280, "h": 720}
    if t is not None:
        h["t"] = t
    return [json.dumps({"msg": h}).encode()]


def april(tick: int, seq: int, t: float, cam: str = "cam1") -> bytes:
    return json.dumps({"type": "apriltag", "v": 1, "cam": cam, "tick": tick,
                       "seq": seq, "t": t, "clock": "est",
                       "ts": {"cap": t, "recv": t + 0.01, "pub": t + 0.02},
                       "n": 0, "tags": []}).encode()


SEAT_ORIGIN_MS = {1: 5_000, 2: 300_000}
"""Each seat sensor's clock at the session's start: unrelated to each other."""


def write_session(path: Path, *, seconds: float = 1.0, seat_hz: float = 20.0,
                  fps: float = 15.0, tm0: int = 1000 * SEC,
                  tw0: float = 1_760_000_000.0, seats: tuple[int, ...] = (1, 2),
                  meta: bool = False, extra: tuple = ()) -> dict:
    """A small session: seat sensors with unrelated clocks (seats 1 and 2 by
    default), one camera, detector records, and a retained status at the
    start. `meta` also writes meta.json's start and end, as the recorder
    does; `extra` adds items of the caller's. Returns what was written, for
    the tests to check against."""
    path.mkdir(parents=True, exist_ok=True)
    w = SessionWriter(path)
    items: list = list(extra)

    def at(t_s: float) -> tuple[int, float]:
        return tm0 + int(t_s * SEC), tw0 + t_s

    tm, tw = at(0.0)
    items.append(MqttMsg(tm, tw, f"rower/{seats[0]}/status", 1, True,
                         b'{"up":true,"seat":%d,"dev":"ab12cd"}' % seats[0]))
    # Seat 1: clock 5 s ahead of the session; seat 2: 300 s; 4 ms delay.
    for seat_n in seats:
        origin_ms = SEAT_ORIGIN_MS.get(seat_n, 100_000 * seat_n)
        n = int(seconds * seat_hz)
        for i in range(n):
            t_s = i / seat_hz + seat_n * 0.003
            tm, tw = at(t_s + 0.004)
            items.append(MqttMsg(tm, tw, f"rower/{seat_n}/seat", 0, False,
                                 seat(i + 1, origin_ms + round(t_s * 1000))))
    for i in range(int(seconds * fps)):
        t_s = i / fps
        tm, tw = at(t_s + 0.030)
        cap = 77_000.0 + t_s * 1000
        items.append(FrameMsg("cam1", tm, tw, cam_pre(i, cap, cap + 9.0),
                              JPEG + bytes([i % 256]),
                              up=(tm - 200_000, tw - 0.0002)))
        tm2, tw2 = at(t_s + 0.050)
        items.append(AprilMsg(tm2, tw2, b"apriltag",
                              april(i + 1, i, tw0 + t_s)))
    items.sort(key=lambda it: it.tm)
    for it in items:
        w.write(it)
    w.close()
    if meta:
        tm1, tw1 = at(seconds + 0.1)
        write_meta(path, {"format": 1, "name": path.name,
                          "start": {"tm": tm0, "tw": tw0},
                          "end": {"tm": tm1, "tw": tw1}})
    return {"items": items, "tm0": tm0, "tw0": tw0}


def write_cam_copy(path: Path, rows: list[tuple[int, float, float]], *,
                   name: str, cam: str = "cam-pi", tw0: float = 1_760_000_000.0,
                   jpeg=None) -> None:
    """A camera's own recording, as cam-sender writes it: `rows` are
    (seq, t_ms, t_send_ms) in send order, and `tm` is t_send_ms in ns on the
    camera's clock. `jpeg(seq)` gives each frame's bytes."""
    path.mkdir(parents=True, exist_ok=True)
    w = SessionWriter(path)
    for seq, t_ms, t_send in rows:
        w.write(FrameMsg(cam, int(round(t_send * 1e6)), tw0 + t_send / 1000,
                         cam_pre(seq, t_ms, t_send, cam=cam),
                         jpeg(seq) if jpeg else JPEG + bytes([seq % 256])))
    w.close()
    write_meta(path, {"format": 1, "writer": "cam-sender", "name": name,
                      "camera": cam})
