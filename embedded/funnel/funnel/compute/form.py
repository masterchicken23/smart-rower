#!/usr/bin/env python3
"""
Form plot: a rower's oar height against seat position, accumulated over the
session, so individual strokes can be seen adding up and outliers stand out.

    x   normalised seat position, 0.0 at the catch, 1.0 at the finish, from
        compute/seat_position.py
    y   oar height. Either the oar tag's height in the camera frame, in pixels
        (`h - center_px[1]`, so up is up), or -- with
        FUNNEL_FORM_USE_HEIGHT_ESTIMATE -- the detector's `oar.height_m`,
        measured against a reference tag (see "Detector contract" below)

The client is handed what it needs to draw the plot, not the time series and
not a picture of it:

  hist      2D histogram per phase (drive, recovery) over the whole session
  mean      the session's mean stroke per phase, with a spread, binned over x
  recent    the last few completed strokes, resampled onto the same x bins
  current   the stroke in progress, raw and decimated
  strokes   one small summary per stroke for the whole session, including a
            deviation score against the mean, for spotting outliers over time

All of it is bounded in size whatever the session length, and it is built
incrementally: each tick folds in only camera frames newer than the last one it
paired.

Pairing
-------
Each camera frame is matched to the seat position at the same instant, by
linear interpolation in the seat pipeline's smoothed series, which
seat_position.transform keeps in `buf.state["series"]`. Both are stamped with
the funnel's monotonic receipt time. The seat series lags the real seat by its
filters (about 0.15 s with the defaults), so a frame received at `t` is paired
with the series at `t + form_seat_lag_s`. A frame waits until the series covers
that instant, and is dropped once it never will: nothing is extrapolated.

Phase and strokes
-----------------
Drive and recovery are told apart from the paired positions themselves, by
turning points with hysteresis (`form_turn_hysteresis`, a fraction of the
slide), as seat_position.py does for the endpoints. The points between an
extreme and the moment the turn is confirmed are held back and assigned once
the direction is known, so the phase split falls at the extreme itself. A
stroke runs from one catch (recovery -> drive) to the next.

Detector contract (vision/april-detect/april_detect/relative.py)
----------------------------------------------------------------
A top-level `oar` object on the `apriltag` record, when the detector has
APRIL_OAR_TAG_ID and APRIL_REF_TAG_ID set:

    "oar": {"oar_id": 0, "ref_id": 7, "dist_m": 0.224,
            "rel_m": [0.216, -0.041, 0.042], "height_m": 0.041,
            "up_axis": "-y", "err_m": 0.001, "ref_ambiguity": 0.04}

`height_m` is the oar tag's centre above the reference tag's centre, along the
reference tag's up axis (APRIL_REF_UP_AXIS), in metres. `null` when either tag
is out of view, absent when the detector is not configured; the funnel treats
both as no sample. /rower/{seat}/oar serves the object as is.

House rules are seat_position.py's: no await, no I/O, never raise on missing
data, state carried in `buf.state`.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from ..config import Settings
from ..store import StreamBuffer
from .seat_position import SERIES_STATE

STATE_KEY = "form"
"""Where a seat's form state lives in its `apriltag` buffer's `state`."""

PAIR_GIVE_UP_S = 2.0
"""A frame still unpaired this long after it arrived never will be: the seat
stream has stopped or is not calibrated."""

STROKE_MIN_S = 0.8
STROKE_MAX_S = 12.0
STROKE_MIN_SPAN = 0.5
"""A catch-to-catch interval only counts as a stroke if it lasts this long and
covers this much of the slide. Shorter ones are fidgets or drills; their samples
still go into the histogram."""

MEAN_MIN_STROKES = 3
"""Strokes the mean needs before a deviation score means anything."""

CURRENT_MAX_POINTS = 80

PHASES = ("drive", "recovery")

STAGES: dict[str, str] = {
    "pair": "active",
    "phase": "active",
    "histogram": "active",
    "strokes": "active",
}


# ---- height ------------------------------------------------------------ #

def _num(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool) \
            and math.isfinite(v):
        return float(v)
    return None


def oar_tag(rec: dict[str, Any], tag_id: Optional[int]) -> Optional[dict]:
    """The tag that is the oar. With an id configured, that tag; without one,
    the only tag in view, and nothing when there are several -- picking one at
    random would mix two tags' heights into one trace."""
    tags = rec.get("tags")
    if not isinstance(tags, list):
        return None
    if tag_id is not None:
        for t in tags:
            if isinstance(t, dict) and t.get("id") == tag_id:
                return t
        return None
    if len(tags) == 1 and isinstance(tags[0], dict):
        return tags[0]
    return None


def height_of(rec: dict[str, Any], s: Settings) -> Optional[float]:
    """The oar height a record carries, in the configured source's units."""
    if s.form_use_height_estimate:
        oar = rec.get("oar")
        return _num(oar.get("height_m")) if isinstance(oar, dict) else None
    tag = oar_tag(rec, s.form_oar_tag_id)
    h = _num(rec.get("h"))
    if tag is None or h is None:
        return None
    c = tag.get("center_px")
    if not isinstance(c, (list, tuple)) or len(c) != 2:
        return None
    cy = _num(c[1])
    return None if cy is None else h - cy


def y_range(rec: dict[str, Any], s: Settings) -> Optional[tuple[float, float]]:
    if s.form_use_height_estimate:
        return (s.form_height_min_m, s.form_height_max_m)
    h = _num(rec.get("h"))
    return (0.0, h) if h else None


# ---- session ----------------------------------------------------------- #

def _bin(v: float, lo: float, hi: float, n: int) -> tuple[int, bool]:
    """Index of `v` in `n` equal bins over [lo, hi], clamped; and whether it
    had to be clamped."""
    i = math.floor((v - lo) / (hi - lo) * n)
    if i < 0:
        return 0, v < lo
    if i >= n:
        return n - 1, v > hi
    return i, False


@dataclass
class Welford:
    n: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def add(self, x: float) -> None:
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self.m2 += d * (x - self.mean)

    @property
    def sd(self) -> Optional[float]:
        return math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else None


@dataclass
class FormSession:
    """Everything accumulated for one seat since its session began."""

    id: int
    t_start: float
    """Monotonic, at the first paired sample (or the reset)."""
    x_bins: int
    y_bins: int
    trace_bins: int
    y_lo: Optional[float] = None
    y_hi: Optional[float] = None
    hist: dict[str, list[list[int]]] = field(default_factory=dict)
    mean: dict[str, list[Welford]] = field(default_factory=dict)
    recent: deque = field(default_factory=deque)
    strokes: deque = field(default_factory=deque)
    n_strokes: int = 0
    n_samples: int = 0
    n_clipped: int = 0
    t_last: Optional[float] = None
    """Monotonic time of the last paired sample."""
    t_last_stroke: Optional[float] = None

    # phase tracker
    dir: int = 0
    """+1 drive (position rising), -1 recovery, 0 not yet known."""
    ext: float = 0.0
    """The running extreme in the current direction (min/max while unknown)."""
    lo: float = 0.0
    hi: float = 0.0
    held: list = field(default_factory=list)
    """Points since the running extreme, whose phase is not yet decided."""
    cur: Optional[dict[str, Any]] = None
    """The stroke in progress: {"t0", "drive": [...], "recovery": [...]}."""

    def __post_init__(self) -> None:
        for p in PHASES:
            self.hist.setdefault(p, [[0] * self.x_bins
                                     for _ in range(self.y_bins)])
            self.mean.setdefault(p, [Welford() for _ in range(self.trace_bins)])


def new_session(state: dict[str, Any], s: Settings, now: float) -> FormSession:
    sid = state.get("next_id", 1)
    state["next_id"] = sid + 1
    sess = FormSession(id=sid, t_start=now, x_bins=s.form_x_bins,
                       y_bins=s.form_y_bins, trace_bins=s.form_trace_points,
                       recent=deque(maxlen=max(s.form_recent_strokes, 0)),
                       strokes=deque(maxlen=max(s.form_max_strokes, 1)))
    state["session"] = sess
    state["rev"] = state.get("rev", 0) + 1
    state.pop("built", None)
    return sess


def _commit(sess: FormSession, phase: str, pts: list) -> None:
    """Points whose phase is now known: into the histogram and the stroke."""
    if not pts:
        return
    h = sess.hist[phase]
    for _t, x, y in pts:
        xi, _ = _bin(x, 0.0, 1.0, sess.x_bins)
        yi, clipped = _bin(y, sess.y_lo, sess.y_hi, sess.y_bins)
        h[yi][xi] += 1
        sess.n_clipped += clipped
    if sess.cur is not None:
        sess.cur[phase].extend(pts)


def _resample(pts: list, n: int) -> list[Optional[float]]:
    """A phase's points -> its height at each of `n` x-bin centres, None where
    the phase never reached.

    Interpolated along the trace rather than averaged per bin: mid-slide the
    seat moves fast enough to cross a bin between two frames, and a plain bin
    average would leave holes exactly where the trace is most informative.
    Where the trace crosses a centre more than once, the crossings are averaged.
    """
    sums = [0.0] * n
    counts = [0] * n
    for (_, x0, y0), (_, x1, y1) in zip(pts, pts[1:], strict=False):
        a, b = min(x0, x1), max(x0, x1)
        i0 = max(0, math.ceil(a * n - 0.5))
        i1 = min(n - 1, math.floor(b * n - 0.5))
        for i in range(i0, i1 + 1):
            c = (i + 0.5) / n
            y = y0 if b == a else y0 + (y1 - y0) * (c - x0) / (x1 - x0)
            sums[i] += y
            counts[i] += 1
    if len(pts) == 1:
        i, _ = _bin(pts[0][1], 0.0, 1.0, n)
        sums[i], counts[i] = pts[0][2], 1
    return [sums[i] / counts[i] if counts[i] else None for i in range(n)]


def _close_stroke(sess: FormSession, t_end: float) -> None:
    """The catch that ends sess.cur: score it against the session mean, then
    fold it in."""
    cur = sess.cur
    sess.cur = None
    if cur is None:
        return
    pts = cur["drive"] + cur["recovery"]
    if not cur["drive"] or not cur["recovery"]:
        return
    dur = t_end - cur["t0"]
    xs = [p[1] for p in pts]
    if (dur < STROKE_MIN_S or dur > STROKE_MAX_S
            or max(xs) - min(xs) < STROKE_MIN_SPAN):
        return

    traces = {p: _resample(cur[p], sess.trace_bins) for p in PHASES}
    sq = 0.0
    k = 0
    for p in PHASES:
        for i, y in enumerate(traces[p]):
            w = sess.mean[p][i]
            if y is not None and w.n >= MEAN_MIN_STROKES:
                sq += (y - w.mean) ** 2
                k += 1
    covered = sum(y is not None for p in PHASES for y in traces[p])
    dev = math.sqrt(sq / k) if k and k * 2 >= covered else None

    for p in PHASES:
        for i, y in enumerate(traces[p]):
            if y is not None:
                sess.mean[p][i].add(y)

    i = sess.n_strokes
    sess.n_strokes += 1
    sess.t_last_stroke = t_end
    t_s = cur["t0"] - sess.t_start
    summary = {"i": i, "t_s": round(t_s, 2), "dur_s": round(dur, 2),
               "dev": None if dev is None else round(dev, 4)}
    sess.strokes.append(summary)
    if sess.recent.maxlen:
        sess.recent.append({**summary, "traces": traces})


def _feed(sess: FormSession, t: float, x: float, y: float,
          s: Settings) -> None:
    """One paired sample through the phase tracker."""
    h = s.form_turn_hysteresis
    p = (t, x, y)
    sess.n_samples += 1

    if sess.t_last is not None and t - sess.t_last > s.seat_reset_gap_s:
        # A dropout: whatever stroke was in progress is not one any more.
        sess.dir, sess.cur, sess.held = 0, None, []
    sess.t_last = t

    if sess.dir == 0:
        if not sess.held:
            sess.lo = sess.hi = x
        sess.lo, sess.hi = min(sess.lo, x), max(sess.hi, x)
        sess.held.append(p)
        if x - sess.lo > h or sess.hi - x > h:
            # Direction found. What came before it is mid-stroke with an
            # unknown phase; it is counted but not drawn.
            sess.dir = 1 if x - sess.lo > h else -1
            sess.ext = x
            sess.held = []
        return

    if sess.dir == 1:                       # drive: watching for the finish
        if x >= sess.ext:
            _commit(sess, "drive", sess.held + [p])
            sess.ext, sess.held = x, []
        elif sess.ext - x > h:
            _commit(sess, "recovery", sess.held + [p])
            sess.dir, sess.ext, sess.held = -1, x, []
        else:
            sess.held.append(p)
        return

    # recovery: watching for the catch
    if x <= sess.ext:
        _commit(sess, "recovery", sess.held + [p])
        sess.ext, sess.held = x, []
    elif x - sess.ext > h:
        catch_t = sess.held[0][0] if sess.held else t
        _close_stroke(sess, catch_t)
        sess.cur = {"t0": catch_t, "drive": [], "recovery": []}
        _commit(sess, "drive", sess.held + [p])
        sess.dir, sess.ext, sess.held = 1, x, []
    else:
        sess.held.append(p)


def _interp(series: deque, t: float, gap_s: float) -> Optional[float]:
    """Position at `t` from the (t, position) series; None across a gap."""
    lo, hi = 0, len(series) - 1
    while lo < hi:                          # first point with time >= t
        mid = (lo + hi) // 2
        if series[mid][0] < t:
            lo = mid + 1
        else:
            hi = mid
    t1, x1 = series[lo]
    if t1 == t or lo == 0:
        return x1 if t1 == t else None
    t0, x0 = series[lo - 1]
    if t1 - t0 > gap_s:
        return None
    return x0 + (x1 - x0) * (t - t0) / (t1 - t0)


# ---- entry point ------------------------------------------------------- #

def update(cam: StreamBuffer, seat: Optional[StreamBuffer], settings: Settings,
           now: float, reset: bool = False) -> dict[str, Any]:
    """Fold one seat's new camera frames into its session, and return the
    published form block (rebuilt only when something changed)."""
    st = cam.state.setdefault(STATE_KEY, {})
    sess: Optional[FormSession] = st.get("session")
    if sess is None or reset:
        sess = new_session(st, settings, now)

    series: deque = (seat.state.get(SERIES_STATE) if seat is not None
                     else None) or deque()
    lag = settings.form_seat_lag_s
    cursor: Optional[float] = st.get("cursor")
    changed = False

    for smp in cam.window(settings.buffer_seconds, now):
        if cursor is not None and smp.t_recv <= cursor:
            continue
        t = smp.t_recv + lag
        if series and t > series[-1][0] and now - smp.t_recv < PAIR_GIVE_UP_S:
            break                           # the seat series has not caught up
        cursor = smp.t_recv
        y = height_of(smp.values, settings)
        if y is None:
            continue                        # no oar in this frame
        x = (_interp(series, t, settings.seat_reset_gap_s)
             if series and series[0][0] <= t <= series[-1][0] else None)
        if x is None:
            st["n_unpaired"] = st.get("n_unpaired", 0) + 1
            continue

        if (sess.t_last is not None
                and smp.t_recv - sess.t_last > settings.form_session_idle_s):
            # Back after a long break: a new outing. Only rolled over when data
            # resumes, so an idle plot stays up for the coach to look at.
            sess = new_session(st, settings, smp.t_recv)
        if sess.y_lo is None:
            rng = y_range(smp.values, settings)
            if rng is None or rng[1] <= rng[0]:
                continue
            sess.y_lo, sess.y_hi = rng
            sess.t_start = smp.t_recv
        _feed(sess, smp.t_recv, x, y, settings)
        st["n_paired"] = st.get("n_paired", 0) + 1
        changed = True
    st["cursor"] = cursor

    if changed:
        st["rev"] = st.get("rev", 0) + 1
    # Rebuilt at most every form_publish_s while rowing, so the published rev
    # (and the ETag) holds still long enough for a polling client to get 304s,
    # and the tick does not rebuild every seat's plot ten times a second. A new
    # session is published at once.
    built = st.get("built")
    if built is None or (built["rev"] != st["rev"] and (
            now - st.get("built_at", 0.0) >= settings.form_publish_s)):
        built = build(sess, st, settings, now)
        st["built"], st["built_at"] = built, now
    return built


def build(sess: FormSession, st: dict[str, Any], s: Settings,
          now: float) -> dict[str, Any]:
    """The client's view of a session. Fresh lists throughout: the result goes
    into a frozen Snapshot and is read unguarded by request handlers."""
    tb = sess.trace_bins
    trace_x = [round((i + 0.5) / tb, 4) for i in range(tb)]

    def pts(trace: list[Optional[float]]) -> list[list[float]]:
        return [[trace_x[i], round(y, 4)]
                for i, y in enumerate(trace) if y is not None]

    def raw(points: list) -> list[list[float]]:
        step = max(1, math.ceil(len(points) / CURRENT_MAX_POINTS))
        return [[round(x, 4), round(y, 4)] for _, x, y in points[::step]]

    cur = sess.cur or {"drive": [], "recovery": []}
    # The wall clock is read only to label the session start for clients;
    # every internal time is monotonic.
    wall = time.time() - (now - sess.t_start)
    return {
        "rev": st["rev"],
        "session": {"id": sess.id, "t_start": round(wall, 3),
                    "n_strokes": sess.n_strokes,
                    "n_samples": sess.n_samples},
        "source": "estimate" if s.form_use_height_estimate else "pixel",
        "units": {"x": "fraction",
                  "y": "m" if s.form_use_height_estimate else "px"},
        "x_range": [0.0, 1.0],
        "y_range": (None if sess.y_lo is None
                    else [sess.y_lo, sess.y_hi]),
        "bins": [sess.x_bins, sess.y_bins],
        "hist": {p: [list(row) for row in sess.hist[p]] for p in PHASES},
        "trace_x": trace_x,
        "mean": {p: {
            "y": [None if w.n == 0 else round(w.mean, 4) for w in sess.mean[p]],
            "sd": [None if w.sd is None else round(w.sd, 4)
                   for w in sess.mean[p]],
            "n": [w.n for w in sess.mean[p]],
        } for p in PHASES},
        "recent": [{"i": r["i"], "t_s": r["t_s"], "dur_s": r["dur_s"],
                    "dev": r["dev"],
                    **{p: pts(r["traces"][p]) for p in PHASES}}
                   for r in sess.recent],
        "current": {p: raw(cur[p]) for p in PHASES},
        "strokes": [dict(x) for x in sess.strokes],
        "n_clipped": sess.n_clipped,
        "n_paired": st.get("n_paired", 0),
        "n_unpaired": st.get("n_unpaired", 0),
        "t_last_recv": sess.t_last,
        "pending": sess.n_samples == 0,
    }
