#!/usr/bin/env python3
"""
Putting every sample and frame of a session on one capture-time axis.

A recording holds two kinds of time for each message:

  * when it *arrived* (`tm`, the Jetson's CLOCK_MONOTONIC), and
  * when it was *captured*, on the sender's own clock: a seat sensor's
    `t_ms` (millis() at the echo), a camera's `t_ms` (sensor timestamp) and
    `t_send_ms` (just before the send).

Arrival times carry Wi-Fi, broker and encoder jitter, so aligning streams by
arrival would smear them by tens of ms. Capture times are exact, but each
sender's clock has its own origin and its own crystal drift.

The mapping is the one the funnel and april-detect already use live -- the
offset `arrival - sender_clock` is the true clock offset plus a transport
delay that is never negative, so its *minimum* is the best estimate of the
offset -- with the advantages of hindsight:

  * the whole session is available, so the minimum is taken in each
    `window_s` window and every sample is mapped from the windows on both
    sides of it, not just from the past;
  * the window minima are joined by straight lines rather than held flat, so
    crystal drift (tens of ppm, i.e. a few ms per minute) is followed, and
    the slope is reported as `drift_ppm`;
  * a sender clock that runs backwards (a reboot) starts a new segment with
    its own mapping, the rule april_detect/clock.py applies live.

For cameras the offset is anchored on `t_send_ms`, as april-detect does, so
the encoder's frame-to-frame variation is not mistaken for network delay; the
capture instant is then `t_ms` through the same mapping.

What it cannot remove: each device's *minimum* one-way delay, which reads as
zero. Two devices are therefore aligned to within the difference of their
minimum delays -- a few ms on Wi-Fi -- against sample periods of 50 ms (seat
sensor) and 67 ms (camera). Running chrony on the senders and sending `t`
removes even that.

AprilTag records need none of this: their `t` is already capture time on the
Jetson's wall clock, so it is moved onto CLOCK_MONOTONIC exactly, with the
recorder's own wall/monotonic pair taken at receipt.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Optional

from .format import U32, Session, frame_header, json_obj, num

WINDOW_S = 10.0

DRIFT_MIN_WINDOWS = 6
"""Drift is reported only for a segment spanning at least this many windows
(a minute at the default). Over less, a 1 ms clock tick is tens of ppm."""

REBOOT_S = 0.5
"""A sender clock that runs backwards by more than this has restarted. Less
is only messages delivered out of order: a reboot takes the whole uptime
back, a reordering only the spread of the delays."""


@dataclass(slots=True)
class Obs:
    tm: int
    """Arrival on the Jetson, CLOCK_MONOTONIC ns (the tee's stamp if teed)."""
    anchor_ms: float
    """Sender clock the offset is measured on (`t_send_ms`, else `t_ms`)."""
    cap_ms: float
    """Sender clock at capture (`t_ms`)."""
    seq: Optional[int]


@dataclass
class DeviceFit:
    device: str
    kind: str
    """"sender": a clock of its own, mapped. "host": already on the Jetson."""
    anchor: str
    seq: list[Optional[int]] = field(default_factory=list)
    capture_s: list[float] = field(default_factory=list)
    """Capture instant of each observation, CLOCK_MONOTONIC seconds."""
    latency_ms: list[float] = field(default_factory=list)
    """Arrival minus mapped send: delay *above the fastest* message nearby
    for a sender; true capture-to-arrival delay for a host-clock device."""
    segments: int = 0
    drift_ppm: Optional[float] = None
    """How fast the sender's clock runs against the Jetson's: +50 means it
    gains 50 us a second. From the longest segment; None if too short."""
    span_s: float = 0.0


# ---- the mapping -------------------------------------------------------- #

def lower_envelope(t: list[float], off: list[float],
                   window_s: float) -> list[tuple[float, float]]:
    """The minimum offset in each window, as (time, offset) points."""
    best: dict[int, tuple[float, float]] = {}
    t0 = t[0]
    for ti, oi in zip(t, off, strict=True):
        k = int((ti - t0) // window_s)
        b = best.get(k)
        if b is None or oi < b[1]:
            best[k] = (ti, oi)
    return [best[k] for k in sorted(best)]


def interp(pts: list[tuple[float, float]], ts: list[float]) -> list[float]:
    """Piecewise-linear through `pts`, extrapolated along the end segments."""
    if len(pts) == 1:
        return [pts[0][1]] * len(ts)
    xs = [p[0] for p in pts]
    out = []
    for t in ts:
        i = min(max(bisect.bisect_right(xs, t), 1), len(pts) - 1)
        (x0, y0), (x1, y1) = pts[i - 1], pts[i]
        out.append(y0 if x1 == x0 else y0 + (y1 - y0) * (t - x0) / (x1 - x0))
    return out


def slope(pts: list[tuple[float, float]]) -> Optional[float]:
    if len(pts) < 2:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if sxx <= 0:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx


def segments(obs: list[Obs]) -> list[list[Obs]]:
    """Split where the sender's clock runs backwards: it rebooted."""
    out: list[list[Obs]] = []
    cur: list[Obs] = []
    high = 0.0
    for o in obs:
        if cur and o.anchor_ms < high - REBOOT_S * 1000.0:
            out.append(cur)
            cur = []
        high = o.anchor_ms if not cur else max(high, o.anchor_ms)
        cur.append(o)
    if cur:
        out.append(cur)
    return out


def fit_sender(device: str, anchor: str, obs: list[Obs],
               window_s: float = WINDOW_S) -> DeviceFit:
    fit = DeviceFit(device, "sender", anchor)
    obs = sorted(obs, key=lambda o: o.tm)
    best_span, best_drift = 0.0, None
    for seg in segments(obs):
        t = [o.tm / 1e9 for o in seg]
        off = [o.tm / 1e9 - o.anchor_ms / 1000.0 for o in seg]
        pts = lower_envelope(t, off, window_s)
        mapped = interp(pts, t)
        for o, oi, mi in zip(seg, off, mapped, strict=True):
            fit.seq.append(o.seq)
            fit.capture_s.append(o.cap_ms / 1000.0 + mi)
            fit.latency_ms.append((oi - mi) * 1000.0)
        span = t[-1] - t[0]
        sl = slope(pts) if len(pts) >= DRIFT_MIN_WINDOWS else None
        if sl is not None and span > best_span:
            # offset = host - sender falls as a fast sender pulls ahead.
            best_span, best_drift = span, -sl * 1e6
        fit.segments += 1
        fit.span_s += span
    fit.drift_ppm = best_drift
    return fit


# ---- reading a session -------------------------------------------------- #

class _Unwrap:
    """uint32 millis wraps every ~49.7 days; keep it monotonic."""

    def __init__(self) -> None:
        self.wraps = 0
        self.last: Optional[float] = None

    def __call__(self, v: float) -> float:
        if self.last is not None and v < self.last - U32 / 2:
            self.wraps += 1
        self.last = v
        return v + self.wraps * U32


def _seq(v: object) -> Optional[int]:
    f = num(v)
    return None if f is None else int(f) % U32


def fit_session(sess: Session, window_s: float = WINDOW_S) -> dict[str, DeviceFit]:
    """Every device in a session, mapped. Keys: the MQTT topic, the camera
    name, or `apriltag:<cam>`."""
    fits: dict[str, DeviceFit] = {}

    # Seat sensors and anything else on MQTT with a clock in its payload.
    mq: dict[str, tuple[str, list[Obs], _Unwrap]] = {}
    for ev in sess.mqtt():
        obj = json_obj(ev.payload)
        if obj is None:
            continue
        t_ms, t = num(obj.get("t_ms")), num(obj.get("t"))
        if t_ms is not None:
            name, val = "t_ms", t_ms
        elif t is not None and t > 0:
            name, val = "t", t * 1000.0
        else:
            continue                        # status, beacons without a clock
        entry = mq.get(ev.source)
        if entry is None:
            entry = mq[ev.source] = (name, [], _Unwrap())
        v = entry[2](val) if name == "t_ms" else val
        entry[1].append(Obs(ev.tm, v, v, _seq(obj.get("seq"))))
    for topic, (name, obs, _) in mq.items():
        fits[topic] = fit_sender(topic, name, obs, window_s)

    # Cameras: offset on t_send_ms, capture at t_ms.
    for cam in sess.cameras():
        obs = []
        anchor = "t_send_ms"
        for ev in sess.frames(cam):
            h = frame_header(ev.pre)
            t_ms, t_send = num(h.get("t_ms")), num(h.get("t_send_ms"))
            a = t_send if t_send is not None else t_ms
            if a is None:
                continue                    # a legacy sender with no timing
            if t_send is None:
                anchor = "t_ms"
            obs.append(Obs(ev.tm_arrival, a, t_ms if t_ms is not None else a,
                           _seq(h.get("seq"))))
        if obs:
            fits[cam] = fit_sender(cam, anchor, obs, window_s)

    # Detector records: already capture time on the Jetson's wall clock.
    for ev in sess.apriltag():
        if ev.source != "apriltag":
            continue
        rec = json_obj(ev.payload)
        t = num(rec.get("t")) if rec else None
        tw = num(ev.rec.get("tw"))
        if rec is None or t is None or tw is None:
            continue
        dev = f"apriltag:{rec.get('cam')}"
        fit = fits.get(dev)
        if fit is None:
            fit = fits[dev] = DeviceFit(dev, "host", "t (host wall)", segments=1)
        cap = t + (ev.tm / 1e9 - tw)        # wall -> monotonic, exact
        fit.seq.append(_seq(rec.get("seq")))
        fit.capture_s.append(cap)
        fit.latency_ms.append((ev.tm / 1e9 - cap) * 1000.0)
    for f in fits.values():
        if f.kind == "host" and len(f.capture_s) > 1:
            f.span_s = f.capture_s[-1] - f.capture_s[0]
    return fits


# ---- comparing two recordings of the same data -------------------------- #

def pct(v: Iterable[float], p: float) -> Optional[float]:
    s = sorted(v)
    if not s:
        return None
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def compare(a: dict[str, DeviceFit], b: dict[str, DeviceFit]) -> dict:
    """Match each device's messages by sequence number across two sessions
    (an original and a recording of its replay) and measure how far each
    device's capture times moved *relative to the others*.

    A replay shifts everything by one constant; alignment is preserved if
    every device moved by that same constant. The constant is the median
    shift over every matched message; the error is what remains.
    """
    per: dict[str, list[float]] = {}
    for dev, fa in a.items():
        fb = b.get(dev)
        if fb is None:
            continue
        first: dict[int, float] = {}
        for s, c in zip(fa.seq, fa.capture_s, strict=True):
            if s is not None and s not in first:
                first[s] = c
        seen: set[int] = set()
        d = []
        for s, c in zip(fb.seq, fb.capture_s, strict=True):
            if s is None or s in seen or s not in first:
                continue
            seen.add(s)
            d.append(c - first[s])
        if d:
            per[dev] = d
    pooled = [x for d in per.values() for x in d]
    if not pooled:
        return {"matched": 0, "devices": {}}
    shift = pct(pooled, 50) or 0.0
    devices = {}
    for dev, d in per.items():
        err = [(x - shift) * 1000.0 for x in d]
        devices[dev] = {
            "matched": len(d),
            "bias_ms": round(pct(err, 50) or 0.0, 3),
            "abs_p95_ms": round(pct([abs(e) for e in err], 95) or 0.0, 3),
        }
    abs_all = [abs(x - shift) * 1000.0 for x in pooled]
    return {
        "matched": len(pooled),
        "shift_s": round(shift, 6),
        "abs_p50_ms": round(pct(abs_all, 50) or 0.0, 3),
        "abs_p95_ms": round(pct(abs_all, 95) or 0.0, 3),
        "max_bias_ms": round(max(abs(v["bias_ms"]) for v in devices.values()), 3),
        "devices": devices,
    }
