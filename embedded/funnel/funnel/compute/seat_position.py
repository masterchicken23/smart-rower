#!/usr/bin/env python3
"""
Seat position: raw distance-sensor millimetres turned into the number the
mobile app displays.

=============================================================================
THIS MODULE IS THE SEAM. Every stage below is an identity no-op right now.
Implement them here; nothing outside this file needs to change.
=============================================================================

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
the pipeline behind it. The output shape below is a pinned contract: the mobile
side can be written against it today, and filling in the stages changes the
numbers without changing the shape. `ready` is false and `pending` lists the
identity stages until that happens, so nobody mistakes an unprocessed reading
for a processed one.

Pipeline order, and why it is this order
----------------------------------------
    calibrate  ->  despike  ->  smooth  ->  normalize
                                        \\->  velocity

Despike before smooth: a single wild sample dragged through a low-pass filter
contaminates many outputs, whereas a median rejects it outright. Velocity after
smooth: differentiation amplifies whatever noise is left. Normalize last,
because it needs the calibrated span and should present the cleaned value.

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
sample here carries a timestamp -- use mm/s and cutoffs in Hz, and the stage
keeps working when a sensor's rate changes.
"""

from __future__ import annotations

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
"""

STAGES: dict[str, str] = {
    "calibrate": "identity",
    "despike": "identity",
    "smooth": "identity",
    "normalize": "identity",
    "velocity": "identity",
}
"""Stage implementation status, reported by the API.

Change a value to "active" in the same commit that implements the stage. When
every stage is active, `/rower/{seat}/position` reports `ready: true`.
"""


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
    """Per-seat reference for turning raw millimetres into slide travel.

    Nothing populates this yet. Two ways to, when the time comes:

      * Auto-range over the session. The extremes of a rowed stroke *are* the
        catch and the finish, so `observed_min_mm`/`observed_max_mm` converge
        within a few strokes with no setup. Needs a plausibility guard, since
        a stuck sensor also has a stable range -- a slide is roughly 600-700 mm
        on an eight, so a 40 mm span is a fault, not a crew.
      * Explicit per-seat config, if a calibration routine is added to the
        rigging workflow. That would belong in Settings, keyed by seat.

    Whichever is chosen, it has to survive a sensor rebooting mid-outing, which
    is why this lives in `buf.state` rather than being recomputed per tick.
    """

    zero_mm: Optional[float] = None
    """Raw reading at the catch (fully compressed)."""
    span_mm: Optional[float] = None
    """Catch-to-finish travel in millimetres."""
    observed_min_mm: Optional[float] = None
    observed_max_mm: Optional[float] = None

    @property
    def usable(self) -> bool:
        return (self.zero_mm is not None and self.span_mm is not None
                and self.span_mm > 0)


# ---- stages ------------------------------------------------------------ #
#
# Each stage takes and returns a list of Readings (oldest first), except the
# two that produce a scalar. All are identity no-ops; the docstrings are the
# specification for implementing them.


def _calibrate(readings: list[Reading], cal: Calibration) -> list[Reading]:
    """Raw sensor distance -> millimetres of travel along the slide.

    Should subtract the seat's own reference so that 0 is the catch and larger
    is further towards the finish, making seats comparable to each other. Also
    the right place to drop readings sitting on SENSOR_CLAMP_MM, which are
    saturated measurements rather than positions, and to update
    `cal.observed_min_mm`/`observed_max_mm` if auto-ranging is the chosen
    approach.

    Note the sensor may be mounted so that distance *decreases* towards the
    finish; the sign belongs here, not in the UI.
    """
    return readings        # identity: not implemented


def _despike(readings: list[Reading]) -> list[Reading]:
    """Remove isolated wild values.

    Ultrasonic rangefinders mismeasure in a characteristic way: most readings
    are good and the occasional one is wrong by a lot, because the echo came
    off the wrong surface or was missed entirely. A running median over a short
    time span (3-5 samples' worth, selected by timestamp rather than by count)
    rejects those without rounding off the catch and finish reversals, which a
    mean would smear.

    Must come before _smooth: one spike through a low-pass filter pollutes many
    subsequent outputs.
    """
    return readings        # identity: not implemented


def _smooth(readings: list[Reading], state: dict[str, Any]) -> list[Reading]:
    """Low-pass the series.

    Derive the filter coefficient from each interval's own `dt` and a cutoff
    expressed in Hz -- a fixed per-sample alpha silently changes its cutoff
    when the publish rate changes, and these sensors do not publish at a
    guaranteed rate. Carry the previous output in `state` (this is that
    stream's `buf.state`) so the filter is continuous across ticks instead of
    restarting every 100 ms.

    Do not over-smooth: the catch and finish reversals are the events stroke
    metrics are built on, and lag at the reversal shows up directly as an
    error in stroke timing. Prefer the gentlest filter that makes the velocity
    usable.
    """
    return readings        # identity: not implemented


def _normalize(travel_mm: Optional[float],
               cal: Calibration) -> Optional[float]:
    """Travel in millimetres -> 0.0 at the catch, 1.0 at the finish.

    This is the value a UI should draw and the only one that is comparable
    across seats, since rowers differ in height and sensors in mounting.

    Return None rather than a guess when the calibration is unusable -- a
    fraction derived from a wrong span is worse than an honest gap, because the
    app cannot tell it is wrong. Clamp to [0, 1] once the span is trustworthy,
    but consider reporting the unclamped value too: a position outside the
    calibrated range means the span needs widening, and silently clamping hides
    that.
    """
    if travel_mm is None or not cal.usable:
        return None        # identity: not implemented (no calibration exists)
    return None


def _velocity(readings: list[Reading]) -> Optional[float]:
    """Along-slide velocity in mm/s, from the filtered series.

    Compute it here rather than trusting the payload's `v_mms`. The device's
    own figure is a 5-sample-lagged difference of *unfiltered* readings
    (pico.ino), and the current ESP sender drops the field before transmission
    anyway (sender.ino sends only `distance_mm`), so it cannot be relied on.

    Fit over a short time span rather than differencing the last two samples;
    a two-point difference at 20 Hz is mostly noise. Sign convention should
    match _calibrate's, and it must be documented wherever it surfaces --
    whether positive means driving or recovering is exactly the kind of thing
    that gets guessed wrong downstream.
    """
    return None            # identity: not implemented


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


def transform(buf: StreamBuffer, settings: Settings,
              now: float) -> dict[str, Any]:
    """Run the pipeline over one seat's buffered samples.

    Returns the pinned `/rower/{seat}/position` block. Shape is stable whether
    or not the stages are implemented, and whether or not any data has
    arrived, so a client never has to special-case startup.
    """
    cal: Calibration = buf.state.setdefault("calibration", Calibration())
    smooth_state: dict[str, Any] = buf.state.setdefault("smooth", {})

    raw = readings_from(buf, settings.buffer_seconds, now)

    series = _calibrate(raw, cal)
    series = _despike(series)
    series = _smooth(series, smooth_state)

    travel_mm = series[-1].d_mm if series else None
    position = _normalize(travel_mm, cal)
    velocity = _velocity(series)

    latest = buf.latest
    age_ms = buf.age_ms(now)
    return {
        "position": position,
        "travel_mm": travel_mm,
        "velocity_mms": velocity,
        "t": None if latest is None or latest.t_src is None
             else round(latest.t_src, 4),
        "age_ms": None if age_ms is None else round(age_ms, 1),
        "stale": is_stale(age_ms, settings.max_age_ms),
        "n_in": len(raw),
        "calibrated": cal.usable,
        "ready": is_ready(),
        "pending": pending_stages(),
    }
