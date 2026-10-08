#!/usr/bin/env python3
"""
The seat-position pipeline.

Two groups. The first pins what must hold whatever the filters do: the output
shape, behaviour on absent and malformed data, and state surviving between
ticks. The second drives the pipeline with synthetic strokes on a synthetic
clock, tick by tick as the compute loop does, and checks the filtering and the
adaptive normalisation against what the trace is known to contain.
"""

from __future__ import annotations

import math
import time

import pytest

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


# ---- synthetic strokes ------------------------------------------------- #

SPM = 28.0
STROKE_S = 60.0 / SPM


def stroke(catch=800.0, finish=200.0):
    """Seat distance over time for a crew whose seat moves towards the sensor
    on the drive (the default mounting): `catch` is the far extreme."""
    mid, amp = (catch + finish) / 2.0, (catch - finish) / 2.0
    return lambda t: mid + amp * math.cos(2.0 * math.pi * t / STROKE_S)


def run(trace, duration, *, hz=20.0, tick_s=0.1, s=None, spikes=None,
        t_start=0.0, buf=None):
    """Feed `trace(t)` at `hz` and call transform every `tick_s`, as the
    compute loop would. `spikes` maps sample index -> replacement d_mm.

    Returns (buf, [(t, block), ...]) so a run can be continued by passing
    `buf` and `t_start` back in.
    """
    s = s or settings()
    spikes = spikes or {}
    if buf is None:
        store = StreamStore(maxlen=2048, window_s=s.buffer_seconds)
        buf = store.buffer(StreamKey("rower", 1, sp.RAW_STREAM))
    base = 1000.0
    out = []
    n = int(round(duration * hz))
    k = 1
    for i in range(n):
        t = t_start + i / hz
        # A sample due exactly on a tick is pushed before it, whatever the
        # rate, so runs at different rates see the same latest instant.
        while t > t_start + k * tick_s + 1e-9:
            tk = t_start + k * tick_s
            out.append((tk, sp.transform(buf, s, base + tk)))
            k += 1
        d = spikes.get(i, trace(t))
        buf.push(Sample(t_recv=base + t, t_src=None, seq=i + 1,
                        values={"d_mm": d}))
    end = t_start + n / hz
    out.append((end, sp.transform(buf, s, base + end)))
    return buf, out


def positions(out, after=0.0):
    return [b["position"] for t, b in out if t >= after]


# ---- output contract --------------------------------------------------- #

def test_shape_is_the_same_with_data_and_without():
    """A client must not have to special-case startup, so the block has every
    field before any sample arrives."""
    empty = sp.transform(buffer_with([]), settings(), time.monotonic())
    full = sp.transform(buffer_with([400, 500, 600]), settings(),
                        time.monotonic())
    assert set(empty) == FIELDS
    assert set(full) == FIELDS
    _, out = run(stroke(), 10.0)
    assert set(out[-1][1]) == FIELDS


def test_no_data_is_stale_and_empty_not_zero():
    """A missing measurement must never be reported as a measurement of 0."""
    b = sp.transform(buffer_with([]), settings(), time.monotonic())
    assert b["position"] is None
    assert b["travel_mm"] is None
    assert b["velocity_mms"] is None
    assert b["n_in"] == 0
    assert b["stale"] is True


def test_every_stage_is_active_and_ready():
    assert all(v == "active" for v in sp.STAGES.values())
    b = sp.transform(buffer_with([500]), settings(), time.monotonic())
    assert b["ready"] is True
    assert b["pending"] == []


def test_ready_follows_the_stage_table(monkeypatch):
    monkeypatch.setitem(sp.STAGES, "velocity", "identity")
    assert sp.is_ready() is False
    b = sp.transform(buffer_with([500]), settings(), time.monotonic())
    assert b["ready"] is False
    assert b["pending"] == ["velocity"]


def test_normalized_position_is_withheld_until_calibrated():
    """A fraction derived from a wrong span is worse than an honest null,
    because the client cannot tell it is wrong."""
    b = sp.transform(buffer_with([300, 400, 500]), settings(),
                     time.monotonic())
    assert b["position"] is None
    assert b["calibrated"] is False


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


def test_clamp_limit_readings_are_dropped():
    """A reading on the sensor clamp is a failed echo, not a position."""
    s = settings()
    lo, hi = sp.SENSOR_CLAMP_MM
    rs = [sp.Reading(0.0, lo), sp.Reading(0.05, 500.0), sp.Reading(0.1, hi),
          sp.Reading(0.15, 1500.0)]
    assert sp._gate(rs, s) == [sp.Reading(0.05, 500.0)]


def test_sensor_clamp_matches_the_firmware_and_the_defaults():
    """pico.ino does `constrain(dist, 20, 1000)`."""
    assert sp.SENSOR_CLAMP_MM == (20.0, 1000.0)
    s = settings()
    assert (s.seat_min_mm, s.seat_max_mm) == sp.SENSOR_CLAMP_MM


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


def test_tick_by_tick_equals_all_at_once():
    """Each sample is filtered exactly once, so how the samples are grouped
    into ticks must not change the result."""
    buf_a, out_a = run(stroke(), 8.0, tick_s=0.1)
    buf_b, out_b = run(stroke(), 8.0, tick_s=100.0)
    assert buf_a.state["calibration"] == buf_b.state["calibration"]
    for k in ("position", "travel_mm", "velocity_mms"):
        assert out_a[-1][1][k] == pytest.approx(out_b[-1][1][k], abs=1e-6)


def test_calibration_is_not_usable_until_span_is_set():
    assert sp.Calibration().usable is False
    assert sp.Calibration(zero_mm=100.0).usable is False
    assert sp.Calibration(zero_mm=100.0, span_mm=0.0).usable is False
    assert sp.Calibration(zero_mm=100.0, span_mm=650.0).usable is True


# ---- normalisation ----------------------------------------------------- #

def test_position_is_null_until_a_full_stroke_then_spans_zero_to_one():
    buf, out = run(stroke(), 30.0)
    early = [b for t, b in out if t < 0.5 * STROKE_S]
    assert early and all(b["position"] is None for b in early)
    assert all(b["calibrated"] is False for b in early)

    last = positions(out, after=30.0 - 2 * STROKE_S)
    assert all(p is not None and 0.0 <= p <= 1.0 for p in last)
    assert min(last) < 0.05
    assert max(last) > 0.95
    assert out[-1][1]["calibrated"] is True

    cal = buf.state["calibration"]
    assert cal.zero_mm == pytest.approx(800.0, abs=30.0)     # catch: far end
    assert cal.finish_mm == pytest.approx(200.0, abs=30.0)


def test_position_follows_the_seat():
    """0 at the catch, 1 at the finish, and in between in between."""
    _, out = run(stroke(), 30.0)
    t_end = out[-1][0]
    # Find the last finish (trace minimum: t = (k + 0.5) * STROKE_S) well
    # after calibration, allowing for the filters' lag.
    k = int(t_end / STROKE_S) - 2
    by_t = dict(out)
    def near(t):
        return min(by_t, key=lambda x: abs(x - t))
    assert by_t[near(k * STROKE_S + 0.15)]["position"] < 0.1
    assert by_t[near((k + 0.5) * STROKE_S + 0.15)]["position"] > 0.9
    mid = by_t[near((k + 0.25) * STROKE_S + 0.15)]["position"]
    assert 0.3 < mid < 0.7


def test_endpoints_adapt_slowly_to_a_new_range():
    s = settings()
    buf, _ = run(stroke(800, 200), 30.0, s=s)
    cal = buf.state["calibration"]
    old_catch = cal.zero_mm

    # One stroke of a shorter reach at the catch barely moves it ...
    buf, _ = run(stroke(700, 200), STROKE_S, s=s, buf=buf, t_start=30.0)
    moved = old_catch - cal.zero_mm
    assert 0.0 <= moved < 0.2 * (old_catch - 700.0)

    # ... and thirty strokes of it settle there.
    buf, _ = run(stroke(700, 200), 30 * STROKE_S, s=s, buf=buf,
                 t_start=30.0 + STROKE_S)
    assert cal.zero_mm == pytest.approx(700.0, abs=25.0)


def test_endpoints_hold_while_the_rower_sits_still_or_fidgets():
    s = settings()
    buf, _ = run(stroke(), 30.0, s=s)
    # The trace ends on a catch, which is a genuine turning point once the
    # seat comes away from it; let that one land before taking the snapshot.
    buf, _ = run(lambda t: 500.0, 2.0, s=s, buf=buf, t_start=30.0)
    cal = buf.state["calibration"]
    before = (cal.zero_mm, cal.finish_mm)

    buf, out = run(lambda t: 500.0, 18.0, s=s, buf=buf, t_start=32.0)
    assert (cal.zero_mm, cal.finish_mm) == before
    assert out[-1][1]["position"] == pytest.approx(
        (before[0] - 500.0) / (before[0] - before[1]), abs=0.01)

    # Wiggles past the turn hysteresis but short of a stroke.
    wiggle = lambda t: 500.0 + 50.0 * math.sin(2.0 * math.pi * t / 2.0)  # noqa: E731
    run(wiggle, 20.0, s=s, buf=buf, t_start=50.0)
    assert (cal.zero_mm, cal.finish_mm) == before


def test_orientation_setting_flips_the_catch():
    s = settings(seat_distance_decreases_to_finish=False)
    buf, out = run(stroke(), 30.0, s=s)
    cal = buf.state["calibration"]
    assert cal.zero_mm == pytest.approx(200.0, abs=30.0)     # catch: near end
    k = int(out[-1][0] / STROKE_S) - 2
    by_t = dict(out)
    t = min(by_t, key=lambda x: abs(x - (k * STROKE_S + 0.15)))
    assert by_t[t]["position"] > 0.9      # far end is now the finish


# ---- filtering --------------------------------------------------------- #

def test_spikes_barely_move_the_output():
    """Ultrasonic failure modes: a wild echo, a run of two, a near-clamp
    reading, and a moderate one small enough to pass the rate gate -- about
    one reading in seven corrupted, far worse than a real sensor.

    Unfiltered, a 950 mm echo would throw the position by most of its range.
    Filtered, the residue is the cost of the *missing* sample: dropping one
    shifts the median by half a sample, and at peak seat speed one sample is
    about 0.075 of the range. So the bound is two samples' worth in the worst
    cluster, and essentially nothing typically."""
    trace = stroke()
    n = int(30.0 * 20)
    spikes = {}
    for i in range(300, n, 37):
        spikes[i] = 950.0
    for i in range(310, n, 53):
        spikes[i] = spikes[i + 1] = 60.0
    for i in range(320, n, 41):
        spikes[i] = trace(i / 20.0) + 150.0
    _, clean = run(trace, 30.0)
    _, dirty = run(trace, 30.0, spikes=spikes)
    diffs = [abs(a[1]["position"] - b[1]["position"])
             for a, b in zip(clean, dirty, strict=True) if a[0] > 15.0]
    assert max(diffs) < 0.15
    assert sorted(diffs)[len(diffs) // 2] < 0.01


def test_rate_gate_relocks_after_a_genuine_step():
    s = settings()
    st: dict = {}
    rs = [sp.Reading(i * 0.05, 600.0) for i in range(10)]
    rs += [sp.Reading(0.5 + i * 0.05, 200.0) for i in range(20)]
    out = sp._despike(rs, st, s)
    assert out[-1].d_mm == 200.0
    # Accepted within a few hundred milliseconds of the step.
    assert any(r.d_mm == 200.0 and r.t < 0.5 + 0.4 for r in out)


def test_output_does_not_depend_on_the_sample_rate():
    """Same physical motion at 20 and 40 Hz. Not bit-identical: the median's
    lag is quantised to the sample interval, which near a reversal is worth a
    few percent of peak velocity."""
    _, slow = run(stroke(), 30.0, hz=20.0)
    _, fast = run(stroke(), 30.0, hz=40.0)
    pairs = [(a[1], b[1]) for a, b in zip(slow, fast, strict=False)
             if a[0] > 15.0 and abs(a[0] - b[0]) < 1e-6]
    assert pairs
    peak_v = max(abs(a["velocity_mms"]) for a, _ in pairs)
    for a, b in pairs:
        assert a["position"] == pytest.approx(b["position"], abs=0.04)
        assert a["velocity_mms"] == pytest.approx(b["velocity_mms"],
                                                  abs=0.1 * peak_v)


def test_velocity_is_positive_on_the_drive():
    """Default mounting: distance falls on the drive, and velocity is
    reported along the slide, so a drive is positive."""
    s = settings()
    rs = [sp.Reading(i * 0.05, 800.0 - 500.0 * i * 0.05) for i in range(10)]
    assert sp._velocity(rs, {}, s) == pytest.approx(500.0, rel=1e-6)
    flipped = settings(seat_distance_decreases_to_finish=False)
    assert sp._velocity(rs, {}, flipped) == pytest.approx(-500.0, rel=1e-6)


def test_a_gap_restarts_the_filters_but_keeps_the_calibration():
    s = settings()
    buf, _ = run(stroke(), 30.0, s=s)
    cal = buf.state["calibration"]
    before = (cal.zero_mm, cal.finish_mm)
    # The sensor reboots: 3 s of nothing, then readings resume elsewhere.
    run(lambda t: 450.0, 0.05, s=s, buf=buf, t_start=33.0)
    assert buf.state["smooth"]["y"] == 450.0      # no memory of before
    assert (cal.zero_mm, cal.finish_mm) == before


def test_position_goes_null_when_every_recent_reading_is_rejected():
    """Readings arriving but all failed: no stale value presented as new."""
    s = settings()
    buf, _ = run(stroke(), 30.0, s=s)
    _, out = run(lambda t: 1000.0, 3.0, s=s, buf=buf, t_start=30.0)
    assert out[-1][1]["position"] is None
    assert out[-1][1]["velocity_mms"] is None
    assert out[-1][1]["n_in"] > 0
