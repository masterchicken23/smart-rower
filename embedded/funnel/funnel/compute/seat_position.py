#!/usr/bin/env python3
"""
Seat position: raw distance-sensor millimetres turned into the number the
mobile app displays.

The app must never be handed the sensor's raw reading. A raw HC-SR04 distance
is not a seat position:

  * Its zero is wherever the sensor happens to be bolted, so it differs per
    seat and changes whenever anything is remounted.
  * It carries isolated wild values -- an echo off the wrong surface, or a
    missed echo that reads as the clamp limit.
  * It is unfiltered, so differentiating it for velocity amplifies noise.
  * It is in millimetres of distance, not fraction of slide travel, which is
    what a UI and a cross-seat comparison actually want.

So the API exposes a *derived* value at `/rower/{seat}/position`, and this is
the pipeline behind it. The output shape is a pinned contract (see
models.SeatPositionResponse).

Pipeline order, and why it is this order
----------------------------------------
    gate -> despike -> smooth -> track endpoints -> travel -> normalize
                              \\-> velocity

  gate        drop readings outside the plausible range (the clamp limits)
  despike     reject readings that imply an impossible seat speed, then take
              a short running median of what is left
  smooth      one-pole low-pass, cutoff in Hz
  endpoints   catch and finish learned from the rower's own turning points,
              adapting slowly over strokes
  travel      millimetres from the catch, positive towards the finish
  normalize   0.0 at the catch, 1.0 at the finish
  velocity    least-squares slope of the smoothed series, mm/s

`gate`, `endpoints` and `travel` together are the "calibrate" stage in STAGES.

Despike before smooth: a single wild sample dragged through a low-pass filter
contaminates many outputs, whereas a gate or median rejects it outright.
Velocity after smooth: differentiation amplifies whatever noise is left.

The filters run in raw distance space and the catch/finish mapping is applied
last. The gate, median and low-pass do not care where zero is, so the order
makes no difference to them, but it means an endpoint moving does not make the
filter state jump.

Incremental by design
---------------------
The compute tick runs every 100 ms over the whole buffered window, but the
filters are stateful, so each tick feeds only readings newer than the last one
it processed (`state["cursor"]`). The result is identical whether the samples
arrive one tick at a time or all at once. A gap longer than
`seat_reset_gap_s` (a sensor reboot, a dropout) restarts the filters but keeps
the learned endpoints, so a seat does not lose its calibration mid-outing.

House rules for anything added here
-----------------------------------
This runs inside the compute tick, on the event loop (see compute/metrics.py).
So: no `await`, no I/O, no blocking. Read samples through `buf.window(...)` and
do not mutate the buffer. Carry state between ticks in `buf.state`, keyed by
stage name. Never raise on absent or partial data -- a sensor that has not
published yet is the normal startup state.

And one trap worth naming, because the existing code in this repo falls into
it: **do not express a threshold or a filter constant per sample.**
`experiments/sensor-esp-pipeline/plotdistance.py` labels drive and recovery
with a 5 mm change between consecutive samples, which silently encodes the
sensor's 20 Hz rate; at 40 Hz the same deadband means twice the velocity. Every
constant here is in mm, mm/s, seconds or Hz, and every filter coefficient is
derived from each interval's own `dt`.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Any, NamedTuple, Optional

from ..config import Settings
from ..store import StreamBuffer, is_stale

RAW_STREAM = "seat"
"""The MQTT stream this pipeline consumes: rower/<seat>/seat."""

OUTPUT = "position"
"""The block name this pipeline produces, served at /rower/<seat>/position."""

VALUE_KEY = "d_mm"
"""The measurement field inside the sample payload."""

SENSOR_CLAMP_MM = (20.0, 1000.0)
"""The range pico.ino clamps its reading into (`constrain(dist, 20, 1000)`).

A value sitting exactly on either limit is a saturated or failed measurement,
not a seat at that position, and should be discarded rather than filtered.
These are the defaults for FUNNEL_SEAT_MIN_MM / FUNNEL_SEAT_MAX_MM.
"""

STAGES: dict[str, str] = {
    "calibrate": "active",
    "despike": "active",
    "smooth": "active",
    "normalize": "active",
    "velocity": "active",
}
"""Stage implementation status, reported by the API under /health. When every
stage is active, `/rower/{seat}/position` reports `ready: true`."""


def pending_stages() -> list[str]:
    return [name for name, status in STAGES.items() if status != "active"]


def is_ready() -> bool:
    return not pending_stages()


class Reading(NamedTuple):
    """One sample reduced to what the stages need.

    `t` is the host monotonic receipt time, which is what ages and rates are
    measured against. Use it as the time base for anything rate-dependent.
    """

    t: float
    d_mm: float


@dataclass
class Calibration:
    """Per-seat catch and finish, learned from the rower's own strokes.

    The extremes of a rowed stroke *are* the catch and the finish, so they are
    taken from turning points in the smoothed series, with no setup. A pair of
    turning points only counts if it is at least `seat_min_span_mm` apart, since
    a stuck sensor or a fidget also has a stable range. The first valid pair sets
    both endpoints outright, and each later turning point moves its own
    endpoint a fraction of the way (see `seat_endpoint_strokes`).

    Lives in `buf.state` so it survives a sensor rebooting mid-outing.
    """

    zero_mm: Optional[float] = None
    """Raw distance at the catch."""
    span_mm: Optional[float] = None
    """Catch-to-finish travel in millimetres."""
    finish_mm: Optional[float] = None
    """Raw distance at the finish."""
    observed_min_mm: Optional[float] = None
    observed_max_mm: Optional[float] = None
    """Extremes of the smoothed series over the stream's life, for debugging."""
    n_turns: int = 0
    """Turning points that have updated an endpoint."""

    @property
    def usable(self) -> bool:
        return (self.zero_mm is not None and self.span_mm is not None
                and self.span_mm > 0)


def _sign(s: Settings) -> float:
    """+1 if raw distance grows towards the finish, -1 if it shrinks."""
    return -1.0 if s.seat_distance_decreases_to_finish else 1.0


# ---- stages ------------------------------------------------------------ #
#
# Each filtering stage takes the readings new since the last tick (oldest
# first) plus its own state dict from `buf.state`, and returns the readings it
# lets through.


def _gate(readings: list[Reading], s: Settings) -> list[Reading]:
    """Drop readings at or beyond the plausible range.

    A reading on the clamp limit is a saturated or missed echo, not a seat
    there, so it is discarded rather than handed to the filters.
    """
    lo, hi = s.seat_min_mm, s.seat_max_mm
    return [r for r in readings if lo < r.d_mm < hi]


def _despike(readings: list[Reading], state: dict[str, Any],
             s: Settings) -> list[Reading]:
    """Remove isolated wild values, in two layers.

    Rate gate: a reading further from the reference than the seat could have
    moved since is rejected outright. The reference is the accepted reading
    that holds the current median, with its own timestamp, not simply the last
    accepted reading: a spike that does get through then cannot drag the gate
    after it and lock out the good readings that follow, and the allowance
    still covers exactly the time elapsed, whatever the sample rate. The limit
    grows with that time, so after a genuine jump the gate widens until the
    new level gets in. It cannot lock out for good.

    Running median over the last `seat_despike_window_s` of accepted readings:
    this catches smaller wild values that are still within the gate. The window
    is a span of time, not a sample count, and a median rejects outliers without
    smearing the reversals the way a mean would.
    """
    out: list[Reading] = []
    hist: deque = state.setdefault("hist", deque())
    for r in readings:
        last: Optional[Reading] = state.get("last")
        if last is not None and r.t - last.t > s.seat_reset_gap_s:
            hist.clear()
            last = None
        if last is not None:
            limit = (s.seat_max_speed_mms * max(r.t - last.t, 0.0)
                     + s.seat_gate_slack_mm)
            if abs(r.d_mm - last.d_mm) > limit:
                state["n_rejected"] = state.get("n_rejected", 0) + 1
                continue
        hist.append(r)
        cutoff = r.t - s.seat_despike_window_s
        while len(hist) > 1 and hist[0].t <= cutoff:
            hist.popleft()
        ranked = sorted(hist, key=lambda x: x.d_mm)
        state["last"] = ranked[(len(ranked) - 1) // 2]
        out.append(Reading(r.t, statistics.median(x.d_mm for x in ranked)))
    return out


def _smooth(readings: list[Reading], state: dict[str, Any],
            s: Settings) -> list[Reading]:
    """One-pole low-pass, continuous across ticks.

    The coefficient comes from each interval's own `dt` and a cutoff in Hz, so
    the filter keeps the same response whatever rate the sensor publishes at.
    The cutoff is deliberately gentle: lag at the reversals turns directly into
    stroke-timing error.
    """
    fc = s.seat_smooth_cutoff_hz
    out: list[Reading] = []
    for r in readings:
        t0: Optional[float] = state.get("t")
        if fc <= 0 or t0 is None or r.t - t0 > s.seat_reset_gap_s:
            y = r.d_mm
        else:
            a = 1.0 - math.exp(-2.0 * math.pi * fc * max(r.t - t0, 0.0))
            y = state["y"] + a * (r.d_mm - state["y"])
        state["t"], state["y"] = r.t, y
        out.append(Reading(r.t, y))
    return out


def _track_endpoints(readings: list[Reading], cal: Calibration,
                     state: dict[str, Any], s: Settings) -> None:
    """Find turning points in the smoothed series and update `cal` from them.

    A turning point is an extreme the series has come back from by more than
    `seat_turn_hysteresis_mm`, stamped with the time it was reached. Until a direction of travel is established,
    nothing is recorded, because the first sample is usually mid-stroke rather
    than at an extreme. After a reset gap the tracker starts over for the same
    reason. When the rower sits still nothing is recorded, so the endpoints
    hold rather than collapsing.
    """
    h = s.seat_turn_hysteresis_mm
    for r in readings:
        x = r.d_mm
        cal.observed_min_mm = (x if cal.observed_min_mm is None
                               else min(cal.observed_min_mm, x))
        cal.observed_max_mm = (x if cal.observed_max_mm is None
                               else max(cal.observed_max_mm, x))

        t0: Optional[float] = state.get("t")
        state["t"] = r.t
        if t0 is None or r.t - t0 > s.seat_reset_gap_s:
            state.update(dir=0, lo=x, hi=x, lo_t=r.t, hi_t=r.t,
                         last_low=None, last_high=None)
            continue

        d = state["dir"]
        if d == 0:                      # direction not yet known
            state["lo"] = min(state["lo"], x)
            state["hi"] = max(state["hi"], x)
            if x - state["lo"] > h:
                state.update(dir=1, hi=x, hi_t=r.t)
            elif state["hi"] - x > h:
                state.update(dir=-1, lo=x, lo_t=r.t)
        elif d == 1:                    # rising: watching for a high extreme
            if x > state["hi"]:
                state.update(hi=x, hi_t=r.t)
            elif state["hi"] - x > h:
                _turning_point(cal, state, s, "high",
                               Reading(state["hi_t"], state["hi"]))
                state.update(dir=-1, lo=x, lo_t=r.t)
        else:                           # falling: watching for a low extreme
            if x < state["lo"]:
                state.update(lo=x, lo_t=r.t)
            elif x - state["lo"] > h:
                _turning_point(cal, state, s, "low",
                               Reading(state["lo_t"], state["lo"]))
                state.update(dir=1, hi=x, hi_t=r.t)


def _turning_point(cal: Calibration, state: dict[str, Any], s: Settings,
                   kind: str, ext: Reading) -> None:
    """Record one extreme and, if it makes a real stroke with the previous
    opposite extreme, move the matching endpoint towards it.

    "A real stroke" means far enough apart in distance (`seat_min_span_mm`)
    and close enough in time (`seat_max_half_stroke_s`): a catch, a long rest,
    then a fidget the other way is not a stroke, even though it spans the
    slide.
    """
    prev: Optional[Reading] = state["last_low" if kind == "high"
                                    else "last_high"]
    state["last_" + kind] = ext
    if (prev is None
            or abs(ext.d_mm - prev.d_mm) < s.seat_min_span_mm
            or ext.t - prev.t > s.seat_max_half_stroke_s):
        return
    value, other = ext.d_mm, prev.d_mm

    # With distance decreasing towards the finish, the catch is the high end.
    is_catch = (kind == "high") == s.seat_distance_decreases_to_finish
    if cal.zero_mm is None or cal.finish_mm is None:
        # Bootstrap: the first real stroke sets both ends outright.
        cal.zero_mm, cal.finish_mm = ((value, other) if is_catch
                                      else (other, value))
    else:
        n = s.seat_endpoint_strokes
        a = 1.0 if n <= 0 else 1.0 - math.exp(-1.0 / n)
        if is_catch:
            cal.zero_mm += a * (value - cal.zero_mm)
        else:
            cal.finish_mm += a * (value - cal.finish_mm)
    cal.span_mm = abs(cal.zero_mm - cal.finish_mm)
    cal.n_turns += 1


def _travel(d_mm: Optional[float], cal: Calibration,
            s: Settings) -> Optional[float]:
    """Smoothed raw distance -> millimetres from the catch, positive towards
    the finish. While uncalibrated, the filtered distance is passed through
    unreferenced, as the response contract describes."""
    if d_mm is None:
        return None
    if not cal.usable:
        return d_mm
    return _sign(s) * (d_mm - cal.zero_mm)


def _normalize(travel_mm: Optional[float],
               cal: Calibration) -> Optional[float]:
    """Travel in millimetres -> 0.0 at the catch, 1.0 at the finish.

    None rather than a guess while the calibration is unusable: a fraction
    from a wrong span is worse than an honest gap. Clamped to [0, 1], because
    the endpoints are averages over strokes and a single stroke reaching past
    one of them is normal.
    """
    if travel_mm is None or not cal.usable:
        return None
    return min(1.0, max(0.0, travel_mm / cal.span_mm))


def _velocity(readings: list[Reading], state: dict[str, Any],
              s: Settings) -> Optional[float]:
    """Along-slide velocity in mm/s, positive towards the finish (the drive),
    negative on the recovery.

    A least-squares slope over the last `seat_velocity_window_s` of the
    smoothed series, rather than a two-point difference, which at 20 Hz is
    mostly noise. Computed here rather than trusting the payload's `v_mms`,
    which is a lagged difference of unfiltered readings, and is not sent by
    every firmware.
    """
    pts: deque = state.setdefault("pts", deque())
    for r in readings:
        if pts and r.t - pts[-1].t > s.seat_reset_gap_s:
            pts.clear()
        pts.append(r)
    if pts:
        cutoff = pts[-1].t - s.seat_velocity_window_s
        while pts and pts[0].t < cutoff:
            pts.popleft()
    if len(pts) < 3:
        return None
    tm = sum(p.t for p in pts) / len(pts)
    dm = sum(p.d_mm for p in pts) / len(pts)
    stt = sum((p.t - tm) ** 2 for p in pts)
    if stt <= 0:
        return None
    slope = sum((p.t - tm) * (p.d_mm - dm) for p in pts) / stt
    return _sign(s) * slope


# ---- entry point ------------------------------------------------------- #

def readings_from(buf: StreamBuffer, window_s: float,
                  now: float) -> list[Reading]:
    """Buffered samples -> Readings, dropping anything without a usable value.

    A sample whose payload has no numeric `d_mm` is skipped rather than
    defaulted: a missing measurement is not a measurement of zero.
    """
    out: list[Reading] = []
    for s in buf.window(window_s, now):
        v = s.values.get(VALUE_KEY)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(Reading(s.t_recv, float(v)))
    return out


def _round(x: Optional[float], nd: int) -> Optional[float]:
    return None if x is None else round(x, nd)


def transform(buf: StreamBuffer, settings: Settings,
              now: float) -> dict[str, Any]:
    """Run the pipeline over one seat's new samples.

    Returns the pinned `/rower/{seat}/position` block. Shape is stable whether
    or not any data has arrived, so a client never has to special-case startup.
    """
    st = buf.state
    cal: Calibration = st.setdefault("calibration", Calibration())
    despike_state: dict[str, Any] = st.setdefault("despike", {})
    smooth_state: dict[str, Any] = st.setdefault("smooth", {})
    turn_state: dict[str, Any] = st.setdefault("turns", {})
    velocity_state: dict[str, Any] = st.setdefault("velocity", {})

    raw = readings_from(buf, settings.buffer_seconds, now)
    cursor: Optional[float] = st.get("cursor")
    new = raw if cursor is None else [r for r in raw if r.t > cursor]
    if new:
        st["cursor"] = new[-1].t

    series = _gate(new, settings)
    series = _despike(series, despike_state, settings)
    series = _smooth(series, smooth_state, settings)
    _track_endpoints(series, cal, turn_state, settings)
    velocity = _velocity(series, velocity_state, settings)

    # The filtered value is only current if it came from recent input: if the
    # stream has gone quiet, or every recent reading was rejected, report an
    # honest gap rather than the last good value as if it were new.
    y_t: Optional[float] = smooth_state.get("t")
    current = (bool(raw) and y_t is not None
               and y_t >= raw[-1].t - settings.seat_reset_gap_s)
    d_smoothed = smooth_state.get("y") if current else None

    travel_mm = _travel(d_smoothed, cal, settings)
    position = _normalize(travel_mm, cal)

    latest = buf.latest
    age_ms = buf.age_ms(now)
    return {
        "position": _round(position, 4),
        "travel_mm": _round(travel_mm, 1),
        "velocity_mms": _round(velocity, 1) if current else None,
        "t": None if latest is None or latest.t_src is None
             else round(latest.t_src, 4),
        "age_ms": None if age_ms is None else round(age_ms, 1),
        "stale": is_stale(age_ms, settings.max_age_ms),
        "n_in": len(raw),
        "calibrated": cal.usable,
        "ready": is_ready(),
        "pending": pending_stages(),
    }
