#!/usr/bin/env python3
"""
The seat-position pipeline.

The stages are identity no-ops, so these tests pin the things that must hold
*regardless* of what the transformation eventually does: the output shape, the
honesty of the `ready`/`pending` flags, behaviour on absent and malformed data,
and the fact that state survives between ticks. They are the harness whoever
implements the stages will work against.
"""

from __future__ import annotations

import time

from funnel.compute import seat_position as sp
from funnel.config import Settings
from funnel.models import Sample, StreamKey
from funnel.store import StreamStore

FIELDS = {"position", "travel_mm", "velocity_mms", "t", "age_ms", "stale",
          "n_in", "calibrated", "ready", "pending"}


def settings(**kw) -> Settings:
    base = {"mqtt_host": "none", "max_age_ms": 1000.0, "buffer_seconds": 10.0}
    base.update(kw)
    return Settings(**base)


def buffer_with(values, *, t0=None, dt=0.05):
    """A seat buffer holding `values` as d_mm readings at `dt` spacing."""
    store = StreamStore(maxlen=1000, window_s=10.0)
    key = StreamKey("rower", 1, sp.RAW_STREAM)
    t0 = time.monotonic() - len(values) * dt if t0 is None else t0
    for i, v in enumerate(values):
        payload = v if isinstance(v, dict) else {"d_mm": v}
        store.push(key, Sample(t_recv=t0 + i * dt, t_src=None, seq=i + 1,
                               values=payload))
    return store.buffer(key)


# ---- output contract --------------------------------------------------- #

def test_shape_is_the_same_with_data_and_without():
    """A client must not have to special-case startup, so the block has every
    field before any sample arrives."""
    empty = sp.transform(buffer_with([]), settings(), time.monotonic())
    full = sp.transform(buffer_with([400, 500, 600]), settings(),
                        time.monotonic())
    assert set(empty) == FIELDS
    assert set(full) == FIELDS


def test_no_data_is_stale_and_empty_not_zero():
    """A missing measurement must never be reported as a measurement of 0."""
    b = sp.transform(buffer_with([]), settings(), time.monotonic())
    assert b["position"] is None
    assert b["travel_mm"] is None
    assert b["velocity_mms"] is None
    assert b["n_in"] == 0
    assert b["stale"] is True


def test_pipeline_declares_itself_unimplemented():
    """The flags must not claim processing that is not happening. When someone
    implements a stage and flips STAGES, this test is what tells them to update
    the expectation deliberately rather than by accident."""
    b = sp.transform(buffer_with([500]), settings(), time.monotonic())
    assert b["ready"] is False
    assert set(b["pending"]) == set(sp.STAGES)
    assert b["calibrated"] is False


def test_ready_follows_the_stage_table(monkeypatch):
    for name in sp.STAGES:
        monkeypatch.setitem(sp.STAGES, name, "active")
    assert sp.is_ready() is True
    assert sp.pending_stages() == []
    b = sp.transform(buffer_with([500]), settings(), time.monotonic())
    assert b["ready"] is True
    assert b["pending"] == []


def test_normalized_position_is_withheld_until_calibrated():
    """A fraction derived from a wrong span is worse than an honest null,
    because the client cannot tell it is wrong."""
    b = sp.transform(buffer_with([300, 400, 500]), settings(),
                     time.monotonic())
    assert b["position"] is None


# ---- input handling ---------------------------------------------------- #

def test_only_numeric_readings_are_taken():
    buf = buffer_with([{"d_mm": 400}, {"d_mm": None}, {"other": 1},
                       {"d_mm": "500"}, {"d_mm": 600}])
    out = sp.transform(buf, settings(), time.monotonic())
    assert out["n_in"] == 2          # the two real numbers


def test_booleans_are_not_numbers():
    """bool is an int subclass in Python; a True must not become 1 mm."""
    buf = buffer_with([{"d_mm": True}, {"d_mm": 450}])
    assert sp.transform(buf, settings(), time.monotonic())["n_in"] == 1


def test_readings_are_oldest_first_with_monotonic_timestamps():
    """Stages are written assuming this ordering and a usable time base --
    anything rate-dependent needs it."""
    buf = buffer_with([100, 200, 300, 400])
    rs = sp.readings_from(buf, 10.0, time.monotonic())
    assert [r.d_mm for r in rs] == [100, 200, 300, 400]
    assert all(a.t <= b.t for a, b in zip(rs, rs[1:], strict=False))


def test_window_is_bounded_by_buffer_seconds():
    """The pipeline must not silently widen its own input window."""
    now = time.monotonic()
    buf = buffer_with(list(range(200)), t0=now - 10.0, dt=0.05)
    out = sp.transform(buf, settings(buffer_seconds=1.0), now)
    assert out["n_in"] <= 21


def test_stale_input_is_flagged_but_still_reported():
    now = time.monotonic()
    buf = buffer_with([400, 450], t0=now - 5.0)
    out = sp.transform(buf, settings(max_age_ms=500.0), now)
    assert out["stale"] is True
    assert out["age_ms"] > 500


# ---- state across ticks ------------------------------------------------ #

def test_state_persists_between_ticks():
    """A filter that restarted every 100 ms tick would be useless, so the
    pipeline's state has to live on the buffer and survive repeated calls."""
    buf = buffer_with([400, 500])
    sp.transform(buf, settings(), time.monotonic())
    first = buf.state["calibration"]
    sp.transform(buf, settings(), time.monotonic())
    assert buf.state["calibration"] is first
    assert "smooth" in buf.state


def test_state_is_per_stream():
    store = StreamStore()
    out = []
    for seat in (1, 2):
        key = StreamKey("rower", seat, sp.RAW_STREAM)
        store.push(key, Sample(t_recv=time.monotonic(), t_src=None, seq=1,
                               values={"d_mm": 400 + seat}))
        sp.transform(store.buffer(key), settings(), time.monotonic())
        out.append(store.buffer(key).state["calibration"])
    assert out[0] is not out[1]


def test_transform_does_not_mutate_the_buffer():
    buf = buffer_with([400, 500, 600])
    before = [s.values["d_mm"] for s in buf.samples]
    sp.transform(buf, settings(), time.monotonic())
    assert [s.values["d_mm"] for s in buf.samples] == before
    assert len(buf.samples) == 3


# ---- stage-level invariants -------------------------------------------- #

def test_stages_are_currently_identity():
    """Documents the no-op state explicitly, so the switch to a real
    implementation is a visible change to this file."""
    rs = [sp.Reading(0.0, 400.0), sp.Reading(0.05, 500.0)]
    cal = sp.Calibration()
    assert sp._calibrate(rs, cal) == rs
    assert sp._despike(rs) == rs
    assert sp._smooth(rs, {}) == rs
    assert sp._velocity(rs) is None


def test_calibration_is_not_usable_until_span_is_set():
    assert sp.Calibration().usable is False
    assert sp.Calibration(zero_mm=100.0).usable is False
    assert sp.Calibration(zero_mm=100.0, span_mm=0.0).usable is False
    assert sp.Calibration(zero_mm=100.0, span_mm=650.0).usable is True


def test_sensor_clamp_matches_the_firmware():
    """pico.ino does `constrain(dist, 20, 1000)`; a reading on either limit is
    a saturated measurement, and _calibrate is told to drop it."""
    assert sp.SENSOR_CLAMP_MM == (20.0, 1000.0)
