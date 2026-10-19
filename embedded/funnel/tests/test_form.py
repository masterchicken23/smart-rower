#!/usr/bin/env python3
"""
The form plot: oar height against seat position over a session.

A synthetic rower drives both streams from one stroke model, on a synthetic
clock, through `metrics.compute` tick by tick as the loop would: the seat
sensor at 20 Hz and the camera at 30 Hz. The oar is low through the drive
(blade in the water) and high on the recovery, so the trace is a loop and the
phase split is visible in the heights.
"""

from __future__ import annotations

import math
import time

import pytest
from httpx import ASGITransport, AsyncClient

from funnel.api.app import create_app
from funnel.compute import form, metrics, seat_position
from funnel.config import Settings
from funnel.models import Sample, Snapshot, StreamKey
from funnel.sources.zmq_apriltag import STREAM as APRILTAG
from funnel.store import StreamStore

SPM = 28.0
STROKE_S = 60.0 / SPM
H_PX = 720
BASE = 1000.0

SEAT = StreamKey("rower", 1, seat_position.RAW_STREAM)
CAM = StreamKey("rower", 1, APRILTAG)


def settings(**kw) -> Settings:
    # Publish every tick, so a test sees each change as soon as it happens.
    base = {"mqtt_host": "none", "max_age_ms": 1000.0, "buffer_seconds": 10.0,
            "form_publish_s": 0.0}
    base.update(kw)
    return Settings(**base)


def seat_mm(t: float) -> float:
    """Catch (800 mm, far from the sensor) at t = 0, finish half a stroke
    later, as in test_seat_position."""
    return 500.0 + 300.0 * math.cos(2.0 * math.pi * t / STROKE_S)


def height_px(t: float, offset: float = 0.0) -> float:
    """Lowest mid-drive, highest mid-recovery."""
    return 300.0 - 60.0 * math.sin(2.0 * math.pi * t / STROKE_S) + offset


def record(t: float, y: float, **kw) -> dict:
    rec = {"type": "apriltag", "v": 1, "cam": "cam1", "t": 1.76e9 + t,
           "w": 1280, "h": H_PX, "n": 1,
           "tags": [{"id": 0, "center_px": [640.0, H_PX - y]}]}
    rec.update(kw)
    return rec


class Rower:
    """Pushes both streams into a store and ticks compute, like the loop."""

    def __init__(self, s: Settings | None = None) -> None:
        self.s = s or settings()
        self.store = StreamStore(maxlen=2048, window_s=self.s.buffer_seconds)
        self.t = 0.0
        self.seq = 0
        self.tick = 0
        self.forms: dict = {}

    def run(self, duration: float, *, tick_s: float = 0.1, cam_hz=30.0,
            seat_hz=20.0, rec=None, outlier=None) -> dict:
        """`rec(t, y)` builds the camera record; `outlier(t)` returns a height
        offset for that instant."""
        rec = rec or record
        events = []
        for i in range(int(round(duration * seat_hz))):
            events.append((self.t + i / seat_hz, "seat"))
        for i in range(int(round(duration * cam_hz))):
            events.append((self.t + i / cam_hz + 1e-4, "cam"))
        events.sort()
        end = self.t + duration
        next_tick = self.t + tick_s
        for t, kind in events:
            while t > next_tick:
                self._compute(next_tick)
                next_tick += tick_s
            self.seq += 1
            if kind == "seat":
                self.store.push(SEAT, Sample(BASE + t, None, self.seq,
                                             {"d_mm": seat_mm(t)}))
            else:
                off = outlier(t) if outlier else 0.0
                self.store.push(CAM, Sample(BASE + t, None, self.seq,
                                            rec(t, height_px(t, off))))
        while next_tick <= end + 1e-9:
            self._compute(next_tick)
            next_tick += tick_s
        self.t = end
        return self.forms.get(1)

    def _compute(self, t: float) -> None:
        self.tick += 1
        *_, self.forms = metrics.compute(self.store, Snapshot.empty(), self.s,
                                         BASE + t)


def hist_total(block: dict, phase: str) -> int:
    return sum(sum(row) for row in block["hist"][phase])


# ---- height extraction ------------------------------------------------- #

def test_pixel_height_is_up_from_the_bottom_of_the_frame():
    assert form.height_of(record(0, 300.0), settings()) == pytest.approx(300.0)


def test_oar_tag_selection():
    two = record(0, 0, n=2, tags=[{"id": 3, "center_px": [0, 100]},
                                  {"id": 0, "center_px": [0, 200]}])
    # No id configured: several tags in view is ambiguous, so no sample.
    assert form.height_of(two, settings()) is None
    assert form.height_of(two, settings(form_oar_tag_id=3)) == H_PX - 100
    assert form.height_of(two, settings(form_oar_tag_id=7)) is None
    assert form.height_of(record(0, 0, n=0, tags=[]), settings()) is None
    # A configured id that is the only tag still has to match.
    assert form.height_of(record(0, 50), settings(form_oar_tag_id=1)) is None


def test_estimate_mode_reads_oar_height_and_ignores_pixels():
    s = settings(form_use_height_estimate=True)
    assert form.height_of(record(0, 300, oar={"height_m": 0.04}), s) == 0.04
    assert form.height_of(record(0, 300, oar=None), s) is None
    assert form.height_of(record(0, 300), s) is None
    assert form.height_of(record(0, 300, oar={"height_m": "x"}), s) is None


# ---- accumulation ------------------------------------------------------ #

def test_pending_until_seat_is_calibrated():
    r = Rower()
    b = r.run(1.0)                      # under one stroke: no calibration
    assert b["pending"] is True
    assert b["session"]["n_samples"] == 0
    assert b["n_unpaired"] > 0
    assert b["y_range"] is None


def test_no_camera_no_form_block():
    r = Rower()
    r.store.push(SEAT, Sample(BASE, None, 1, {"d_mm": 500}))
    r._compute(0.1)
    assert r.forms == {}


def test_session_builds_a_loop():
    r = Rower()
    b = r.run(40.0)
    assert b["pending"] is False
    assert b["y_range"] == [0.0, H_PX]
    assert b["units"] == {"x": "fraction", "y": "px"}
    n_strokes = b["session"]["n_strokes"]
    assert 40.0 / STROKE_S - 5 <= n_strokes <= 40.0 / STROKE_S

    # Histogram counts committed samples only, never more than were paired.
    total = hist_total(b, "drive") + hist_total(b, "recovery")
    assert 0.9 * b["n_paired"] < total <= b["n_paired"]
    assert len(b["hist"]["drive"]) == r.s.form_y_bins
    assert len(b["hist"]["drive"][0]) == r.s.form_x_bins

    # Mid-slide, the drive is low and the recovery high, close to the model.
    mid = r.s.form_trace_points // 2
    d, rc = b["mean"]["drive"]["y"][mid], b["mean"]["recovery"]["y"][mid]
    assert d < 260 < 340 < rc
    assert b["mean"]["drive"]["n"][mid] >= n_strokes - 2

    assert len(b["recent"]) == r.s.form_recent_strokes
    assert b["recent"][-1]["i"] == n_strokes - 1
    assert len(b["strokes"]) == n_strokes
    for st in b["strokes"]:
        assert st["dur_s"] == pytest.approx(STROKE_S, abs=0.2)


def test_outlier_stroke_scores_highest():
    r = Rower()
    t_bad = (12.3 * STROKE_S, 13.0 * STROKE_S)      # one recovery, raised
    b = r.run(40.0, outlier=lambda t: 80.0 if t_bad[0] <= t < t_bad[1] else 0)
    scored = [s for s in b["strokes"] if s["dev"] is not None]
    assert len(scored) > 10
    worst = max(scored, key=lambda s: s["dev"])
    others = sorted(s["dev"] for s in scored if s is not worst)
    # Folded into a short session's mean it raises the later strokes' scores a
    # little, but it still stands well clear of all of them.
    assert worst["dev"] > 5 * others[-1]
    assert worst["dev"] == pytest.approx(80.0 * 0.7, rel=0.3)


def test_independent_of_tick_grouping():
    a = Rower().run(30.0, tick_s=0.1)
    b = Rower().run(30.0, tick_s=0.5)
    assert a["hist"] == b["hist"]
    assert a["session"]["n_strokes"] == b["session"]["n_strokes"]
    assert a["mean"] == b["mean"]


def test_rev_only_changes_with_data():
    r = Rower()
    b1 = r.run(10.0)
    r._compute(r.t + 0.1)
    b2 = r.forms[1]
    assert b2 is b1                     # no new frames: the same object reused
    b3 = r.run(1.0)
    assert b3["rev"] > b1["rev"]


def test_publish_is_throttled_but_a_reset_is_not():
    r = Rower(settings(form_publish_s=0.5))
    r.run(10.0)
    revs = []
    for _ in range(10):
        r.run(0.1)
        revs.append(r.forms[1]["rev"])
    # New frames every tick, but the published plot changes every 0.5 s.
    assert 2 <= len(set(revs)) <= 3
    r.store.form_reset.add(1)
    r._compute(r.t + 0.01)
    assert r.forms[1]["session"]["id"] == 2


def test_estimate_mode_end_to_end():
    s = settings(form_use_height_estimate=True)

    def rec(t, y):
        oar = None if int(t * 10) % 7 == 0 else {"height_m": (y - 300) / 1000}
        return record(t, y, oar=oar)

    b = Rower(s).run(30.0, rec=rec)
    assert b["source"] == "estimate" and b["units"]["y"] == "m"
    assert b["y_range"] == [-0.30, 0.30]
    mid = s.form_trace_points // 2
    assert b["mean"]["drive"]["y"][mid] < -0.03
    assert b["mean"]["recovery"]["y"][mid] > 0.03
    assert b["n_clipped"] == 0


def test_reset_starts_a_new_session():
    r = Rower()
    b = r.run(20.0)
    assert b["session"]["id"] == 1 and b["session"]["n_strokes"] > 0
    r.store.form_reset.add(1)
    r._compute(r.t + 0.05)
    b = r.forms[1]
    assert b["session"]["id"] == 2
    assert b["session"]["n_strokes"] == 0 and b["pending"] is True
    assert not r.store.form_reset
    b = r.run(10.0)
    assert b["session"]["id"] == 2 and b["session"]["n_strokes"] > 0


def test_idle_gap_rolls_over_only_when_data_resumes():
    r = Rower(settings(form_session_idle_s=30.0))
    first = r.run(20.0)
    r.t += 40.0                          # nothing for 40 s
    r._compute(r.t)
    assert r.forms[1]["session"]["id"] == 1     # idle: still served
    b = r.run(10.0)
    assert b["session"]["id"] == 2
    assert b["session"]["n_samples"] < first["session"]["n_samples"]


# ---- API --------------------------------------------------------------- #

def make_app(**kw):
    base = {"mqtt_host": "127.0.0.1", "max_age_ms": 1000.0,
            "record_enabled": False, "form_publish_s": 0.0}
    base.update(kw)
    app = create_app(Settings(**base))
    return app, app.state.parts


def fill(parts, seconds=9.0):
    """Push a synthetic session through the app's own store and loop, on the
    real monotonic clock (so ages and staleness behave)."""
    store = parts["store"]
    t0 = time.monotonic() - seconds
    for seq, i in enumerate(range(int(seconds * 30)), start=1):
        t = i / 30.0
        if i % 3 != 2:                              # 20 Hz seat
            store.push(SEAT, Sample(t0 + t, None, seq, {"d_mm": seat_mm(t)}))
        store.push(CAM, Sample(t0 + t + 1e-4, None, seq,
                               record(t, height_px(t))))
    parts["loop"].tick_once()


async def get(app, path, **headers):
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://funnel.test") as c:
        return await c.get(path, headers=headers)


async def post(app, path):
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://funnel.test") as c:
        return await c.post(path)


@pytest.mark.asyncio
async def test_form_404_without_camera():
    app, parts = make_app()
    parts["loop"].tick_once()
    r = await get(app, "/rower/1/form")
    assert r.status_code == 404
    assert (await post(app, "/rower/1/form/reset")).status_code == 404


@pytest.mark.asyncio
async def test_form_pending_then_filled():
    app, parts = make_app()
    # Older than anything fill() pushes, so it does not move the cursor past it.
    parts["store"].push(CAM, Sample(time.monotonic() - 9.5, None, 1,
                                    record(0, 300)))
    parts["loop"].tick_once()
    r = await get(app, "/rower/1/form")
    assert r.status_code == 200
    assert r.json()["pending"] is True

    fill(parts)
    r = await get(app, "/rower/1/form")
    body = r.json()
    assert r.status_code == 200
    assert body["seat"] == 1 and body["pending"] is False
    assert body["session"]["n_strokes"] > 0
    assert body["age_ms"] is not None
    assert "t_last_recv" not in body
    assert r.headers["etag"] == f'"1-{body["rev"]}"'

    r = await get(app, "/rower/1/form?strokes=false")
    assert "strokes" not in r.json()


@pytest.mark.asyncio
async def test_form_etag_304():
    app, parts = make_app()
    fill(parts)
    r = await get(app, "/rower/1/form")
    etag = r.headers["etag"]
    r2 = await get(app, "/rower/1/form", **{"If-None-Match": etag})
    assert r2.status_code == 304 and r2.content == b""
    assert r2.headers["etag"] == etag
    parts["store"].form_reset.add(1)
    parts["loop"].tick_once()
    r3 = await get(app, "/rower/1/form", **{"If-None-Match": etag})
    assert r3.status_code == 200


@pytest.mark.asyncio
async def test_form_reset_endpoint():
    app, parts = make_app()
    fill(parts)
    r = await post(app, "/rower/1/form/reset")
    assert r.status_code == 202 and r.json() == {"seat": 1, "session": 2}
    parts["loop"].tick_once()
    body = (await get(app, "/rower/1/form")).json()
    assert body["session"]["id"] == 2
    assert body["session"]["n_strokes"] == 0


@pytest.mark.asyncio
async def test_form_kept_out_of_snapshot_and_listed():
    app, parts = make_app()
    fill(parts)
    snap = (await get(app, "/snapshot")).json()
    assert "form" not in snap
    assert "form" not in snap["rowers"]["1"]
    assert "/rower/{seat}/form" in (await get(app, "/")).json()["endpoints"]
    health = (await get(app, "/health")).json()
    assert health["pipelines"]["form"]["height"] == "pixel"


@pytest.mark.asyncio
async def test_form_disabled():
    app, parts = make_app(form_enabled=False)
    fill(parts)
    assert (await get(app, "/rower/1/form")).status_code == 404


def test_empty_oar_tag_id_in_env_means_unset(monkeypatch):
    monkeypatch.setenv("FUNNEL_FORM_OAR_TAG_ID", "")
    assert Settings(mqtt_host="none").form_oar_tag_id is None
