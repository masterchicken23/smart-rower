#!/usr/bin/env python3
"""
Play several recorded sessions at once, as one crew: each session's rowers
and cameras moved onto seats and cameras of their own, every session placed
on a shared timeline by an offset. Two single-rower sessions recorded
minutes apart become a two-seat boat for the funnel and april-detect.

    python -m recorder.mix data/A data/B --offset -3.2
    python -m recorder.mix data/A data/B --offset auto --dry-run
    python -m recorder.mix data/A data/B --offset auto --cam-copy pi/ --loop

What it does to each session, and nothing more -- the timing is
recorder.play's engine, one track per session, so every alignment guarantee
of a single replay holds within each session:

  * Seats. The first session keeps its seats. Each later one's seats move,
    in order, past the highest seat already in use: two sessions of seat 1
    play as seats 1 and 2. `rower/1/seat` becomes `rower/2/seat`, and a
    status payload's `seat` follows.
  * Cameras. The same rule: the first session keeps its camera names; each
    later one's become the next free `camN` (cam1 -> cam2, on tcp://*:5582).
    Detector records are renamed to match (the funnel files them by `cam`).
    A session's cameras are those with video, or with detections, in it.
    Records of the idle detectors (apriltag_meta only, no camera) are left
    out, or they would land on another session's camera.
  * Other topics (boat/imu, beacons) come from the first session only: one
    boat.
  * Time. `--offset S` puts a session's start S seconds after the first
    session's start; negative puts it before. One per session after the
    first. "Start" is meta.json's `start`, the moment the recorder began
    the session -- so an offset does not move with --no-video or --start.
    To line up a catch at 10.0 s in A with one at 13.2 s in B, B goes 3.2 s
    earlier: `--offset -3.2`.
  * `--offset auto` finds the offset that lines the strokes up: the lag
    with the best correlation between the two sessions' seat-distance
    traces, within --search seconds. Strokes repeat, so there is a peak
    every stroke; the best and the runners-up are printed, and any of them
    can be given by hand instead.
  * `--start`/`--end` count on the shared timeline. With --loop the crew
    loops as a unit, the shortest session going quiet until the longest
    ends.

Payload clocks are moved per session by the engine, so the two copies of
the same seat sensor (or camera) never share a clock: each moved seat is a
separate stream to the funnel, with an estimator of its own.

Point the consumers at it as for recorder.play; the printout at start says
what to set for the cameras and seats it made.
"""

from __future__ import annotations

import argparse
import math
import re
import signal
import statistics
import sys
from dataclasses import dataclass, field
from typing import Optional

from . import play as engine
from .format import Session, json_obj, num
from .log import log
from .play import (
    Track,
    add_playback_args,
    copies_for,
    default_video_binds,
    finish_playback_args,
    prescan,
    topic_seat,
)
from .sync import Obs, _Unwrap, fit_sender

GRID_HZ = 20.0
"""The seat traces are compared on this grid (the seat sensor's rate)."""
MEDIAN_N = 5
"""Running median over this many samples first: takes out the seat sensor's
single-sample no-echo spikes (~1030 mm)."""
MAX_GAP_S = 0.5
"""A gap in a trace longer than this is not interpolated across."""
MIN_OVERLAP_S = 10.0
"""Lags where the traces overlap for less than this are not considered."""
DEFAULT_SEARCH_S = 20.0

CAM_NUMBER = re.compile(r"(\d+)$")


# ---- what each session holds -------------------------------------------- #

def cam_number(cam: str) -> Optional[int]:
    m = CAM_NUMBER.search(cam)
    return int(m[1]) if m else None


def _cam_order(cam: str) -> tuple[str, int]:
    n = cam_number(cam)
    return (cam[: len(cam) - len(str(n))] if n is not None else cam, n or 0)


def discover(sess: Session) -> tuple[list[int], list[str]]:
    """(seats, cameras) the session holds: `rower/<n>/...` topics; cameras
    with video, or named by detections (not only by apriltag_meta)."""
    seats: set[int] = set()
    for ev in sess.mqtt():
        s = topic_seat(ev.source)
        if s is not None and s >= 1:
            seats.add(s)
    cams = set(sess.cameras())
    for ev in sess.apriltag():
        if ev.source == "apriltag":
            cam = (json_obj(ev.payload) or {}).get("cam")
            if isinstance(cam, str) and cam and "/" not in cam:
                cams.add(cam)
    return sorted(seats), sorted(cams, key=_cam_order)


def assign(found: list[tuple[list[int], list[str]]]
           ) -> list[tuple[dict[int, int], dict[str, str]]]:
    """Seat and camera maps per session: the first as recorded, each later
    one moved past the highest seat and camera number already in use."""
    used_seats: set[int] = set()
    used_cams: set[str] = set()
    out: list[tuple[dict[int, int], dict[str, str]]] = []
    for i, (seats, cams) in enumerate(found):
        if i == 0:
            sm = {s: s for s in seats}
            cm = {c: c for c in cams}
        else:
            top = max(used_seats, default=0)
            sm = {s: top + 1 + j for j, s in enumerate(seats)}
            k = max((n for c in used_cams if (n := cam_number(c)) is not None),
                    default=0)
            cm = {}
            for c in cams:
                k += 1
                while f"cam{k}" in used_cams:
                    k += 1
                cm[c] = f"cam{k}"
        used_seats |= set(sm.values())
        used_cams |= set(cm.values())
        out.append((sm, cm))
    return out


def session_origin(sess: Session) -> int:
    """The session's start on its recorded clock: meta.json's, else its
    first message."""
    tm = (sess.meta.get("start") or {}).get("tm")
    if isinstance(tm, int):
        return tm
    firsts = [next(iter(it), None) for it in
              (sess.mqtt(), sess.apriltag(), *(sess.frames(c) for c in sess.cameras()))]
    tms = [ev.tm for ev in firsts if ev is not None]
    if not tms:
        raise SystemExit(f"[mix] {sess.name}: the session is empty")
    return min(tms)


# ---- lining the strokes up ---------------------------------------------- #

def seat_trace(sess: Session, seat: int, origin: int) -> list[tuple[float, float]]:
    """(seconds from `origin`, d_mm) for one seat, at each sample's capture
    time: the sensor's `t_ms` mapped onto the recorder's clock (sync.py),
    median-filtered."""
    topic = f"rower/{seat}/seat"
    rows: list[tuple[int, float, float]] = []
    unwrap = _Unwrap()
    for ev in sess.mqtt():
        if ev.source != topic:
            continue
        obj = json_obj(ev.payload) or {}
        t_ms, d = num(obj.get("t_ms")), num(obj.get("d_mm"))
        if t_ms is None or d is None:
            continue
        rows.append((ev.tm, unwrap(t_ms), d))
    if not rows:
        return []
    rows.sort(key=lambda r: r[0])
    fit = fit_sender(topic, "t_ms", [Obs(tm, v, v, None) for tm, v, _ in rows])
    pts = sorted(zip(fit.capture_s, (d for _, _, d in rows), strict=True))
    h = MEDIAN_N // 2
    vals = [d for _, d in pts]
    med = [statistics.median(vals[max(0, i - h): i + h + 1]) for i in range(len(vals))]
    return [(t - origin / 1e9, v) for (t, _), v in zip(pts, med, strict=True)]


def resample(trace: list[tuple[float, float]], hz: float = GRID_HZ
             ) -> tuple[int, list[Optional[float]]]:
    """Linear interpolation onto t = n / hz: (first n, values). None where
    the trace has a gap longer than MAX_GAP_S."""
    if len(trace) < 2:
        return 0, []
    n0 = math.ceil(trace[0][0] * hz)
    n1 = math.floor(trace[-1][0] * hz)
    out: list[Optional[float]] = []
    j = 0
    for n in range(n0, n1 + 1):
        t = n / hz
        while j < len(trace) - 2 and trace[j + 1][0] <= t:
            j += 1
        (ta, va), (tb, vb) = trace[j], trace[j + 1]
        if tb - ta > MAX_GAP_S:
            out.append(None)
        else:
            out.append(va if tb <= ta else va + (vb - va) * (t - ta) / (tb - ta))
    return n0, out


def _pearson(xs: list[Optional[float]], ys: list[Optional[float]],
             min_n: int) -> Optional[float]:
    n = sx = sy = sxx = syy = sxy = 0.0
    for x, y in zip(xs, ys, strict=True):
        if x is None or y is None:
            continue
        n += 1
        sx += x
        sy += y
        sxx += x * x
        syy += y * y
        sxy += x * y
    if n < min_n:
        return None
    vx, vy = n * sxx - sx * sx, n * syy - sy * sy
    if vx <= 0 or vy <= 0:
        return None
    return (n * sxy - sx * sy) / math.sqrt(vx * vy)


@dataclass
class Suggestion:
    offset_s: float
    r: float
    others: list[tuple[float, float]] = field(default_factory=list)
    """The next-best peaks: (offset, r)."""

    def describe(self) -> str:
        s = f"{self.offset_s:+.3f} s (r {self.r:.2f})"
        if self.others:
            s += "; next peaks " + ", ".join(f"{o:+.2f} s (r {r:.2f})"
                                             for o, r in self.others)
        return s


def suggest_offset(a: list[tuple[float, float]], b: list[tuple[float, float]],
                   search_s: float = DEFAULT_SEARCH_S, hz: float = GRID_HZ,
                   n_others: int = 3) -> Optional[Suggestion]:
    """The offset to give trace `b` (seconds from its own origin) so that it
    best matches trace `a`: b's time u plays at a's time u + offset."""
    na0, av = resample(a, hz)
    nb0, bv = resample(b, hz)
    if not av or not bv:
        return None
    kmax = int(round(search_s * hz))
    min_n = int(MIN_OVERLAP_S * hz)
    rs: list[Optional[float]] = []
    for k in range(-kmax, kmax + 1):
        # B's sample j plays at grid point nb0 + j + k, which is A's i = j + s.
        s = nb0 + k - na0
        j0, j1 = max(0, -s), min(len(bv), len(av) - s)
        rs.append(_pearson(av[j0 + s: j1 + s], bv[j0:j1], min_n) if j1 > j0 else None)

    peaks = []
    for i in range(1, len(rs) - 1):
        y0, y1, y2 = rs[i - 1], rs[i], rs[i + 1]
        if y0 is None or y1 is None or y2 is None or y1 < y0 or y1 < y2:
            continue
        den = y0 - 2 * y1 + y2
        d = 0.0 if den >= 0 else max(-0.5, min(0.5, 0.5 * (y0 - y2) / den))
        peaks.append((round((i - kmax + d) / hz, 3), y1))
    if not peaks:
        return None
    peaks.sort(key=lambda p: -p[1])
    best = peaks[0]
    return Suggestion(best[0], best[1], [(o, round(r, 3)) for o, r in peaks[1:1 + n_others]])


# ---- the crew ----------------------------------------------------------- #

@dataclass
class Member:
    sess: Session
    origin: int
    seats: list[int]
    cams: list[str]
    seat_map: dict[int, int] = field(default_factory=dict)
    cam_map: dict[str, str] = field(default_factory=dict)
    offset_s: float = 0.0
    auto: Optional[Suggestion] = None


def parse_offsets(raw: list[str], n: int) -> list[Optional[float]]:
    """--offset values for sessions 2..n: a number, or None for `auto`."""
    if len(raw) > n - 1:
        raise ValueError(f"{len(raw)} offsets for {n - 1} session(s) after the first")
    out: list[Optional[float]] = []
    for v in raw:
        if v.strip().lower() == "auto":
            out.append(None)
            continue
        f = float(v)
        if not math.isfinite(f):
            raise ValueError(f"not a number of seconds: {v!r}")
        out.append(f)
    return out + [0.0] * (n - 1 - len(out))


def _trace(m: Member) -> list[tuple[float, float]]:
    for seat in m.seats:
        tr = seat_trace(m.sess, seat, m.origin)
        if len(tr) > 1:
            return tr
    raise SystemExit(f"[mix] --offset auto: {m.sess.name} has no seat-sensor "
                     "samples (rower/<n>/seat with t_ms and d_mm)")


def crew(sessions: list[Session], args: argparse.Namespace) -> list[Member]:
    found = [discover(s) for s in sessions]
    members = [Member(s, session_origin(s), seats, cams)
               for s, (seats, cams) in zip(sessions, found, strict=True)]
    for m, (sm, cm) in zip(members, assign(found), strict=True):
        m.seat_map, m.cam_map = sm, cm
    offsets = parse_offsets(args.offset, len(sessions))
    first: Optional[list[tuple[float, float]]] = None
    for m, off in zip(members[1:], offsets, strict=True):
        if off is None:
            if first is None:
                first = _trace(members[0])
            sug = suggest_offset(first, _trace(m), args.search)
            if sug is None:
                raise SystemExit(
                    f"[mix] --offset auto: no lag within ±{args.search:g} s lines "
                    f"up {m.sess.name}'s strokes with {members[0].sess.name}'s "
                    f"(they need {MIN_OVERLAP_S:g} s of overlap); give --offset")
            m.offset_s, m.auto = sug.offset_s, sug
        else:
            m.offset_s = off
    return members


def tracks(members: list[Member], args: argparse.Namespace) -> list[Track]:
    base = min(m.offset_s for m in members)
    out = []
    for i, m in enumerate(members):
        at_s = m.offset_s - base
        video = [] if args.no_video else [c for c in m.cams if c in m.sess.cameras()]
        out.append(Track(
            m.sess, mqtt=not args.no_mqtt, apriltag=args.apriltag is not None,
            cameras=video, start_s=args.start - at_s,
            end_s=None if args.end is None else args.end - at_s,
            origin=m.origin, at_s=at_s, seats=m.seat_map, cams=m.cam_map,
            other_topics=i == 0, copies=copies_for(m.sess, args, video, "[mix]")))
    return out


def _local(endpoint: str) -> str:
    return endpoint.replace("*", "127.0.0.1").replace("0.0.0.0", "127.0.0.1")


def describe(members: list[Member], trks: list[Track],
             args: argparse.Namespace) -> None:
    for m, t in zip(members, trks, strict=True):
        prescan(t)
        moves = [f"rower/{a} -> rower/{b}" for a, b in m.seat_map.items()]
        moves += [f"{a} -> {b}" for a, b in m.cam_map.items()]
        how = f" (auto: {m.auto.describe()})" if m.auto else ""
        log(f"[mix] {m.sess.name}: offset {m.offset_s:+.3f} s{how}")
        log(f"[mix]   plays as  {', '.join(moves) or 'nothing'}")
        if t.plan.n:
            log(f"[mix]   {t.plan.span_ns / 1e9:.1f} s from "
                f"+{t.pos(t.plan.t0) / 1e9:.3f} s on the shared timeline, "
                + ", ".join(f"{v} {k}" for k, v in sorted(t.plan.n.items())))
        else:
            log("[mix]   nothing in the window")
        for cam in t.cameras:
            c = t.copies.get(cam)
            src = (f"{c.cam}'s own copy, {c.count()} frames" if c is not None
                   else f"the Jetson's copy, {sum(1 for _ in m.sess.frames(cam))} frames")
            log(f"[mix]   {cam} video: {src}")

    log("[mix] consumers:")
    pairs = [(next(iter(m.cam_map.values())), next(iter(m.seat_map.values())))
             for m in members if len(m.cam_map) == 1 and len(m.seat_map) == 1]
    if len(pairs) == len(members):
        log("[mix]   funnel        FUNNEL_APRILTAG_CAMERAS="
            + ",".join(f"{c}:{s}" for c, s in pairs))
    else:
        log("[mix]   funnel        map each camera to its rower's seat in "
            "FUNNEL_APRILTAG_CAMERAS (cameras: "
            + ", ".join(c for m in members for c in m.cam_map.values()) + ")")
    if args.apriltag is not None:
        log(f"[mix]   funnel        FUNNEL_APRILTAG_CONNECT={_local(args.apriltag)} "
            "(the recorded detections; do not run april-detect)")
    cams = [c for t in trks for c in (t.played_cam(x) for x in t.cameras) if c]
    if cams:
        binds = default_video_binds(cams)
        binds.update(args.video_bind)
        for c in cams:
            n = cam_number(c)
            var = f"CAM{n}_SOURCE" if c == f"cam{n}" else f"source for {c}"
            log(f"[mix]   april-detect  {var}={_local(binds[c])} "
                f"(APRIL_CAMERA_ID={c})")


# ---- the command -------------------------------------------------------- #

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Play several recorded sessions at once, as one crew.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("sessions", nargs="+", metavar="session",
                   help="session directories; the first keeps its seats and "
                        "cameras, each later one moves to the next free ones")
    p.add_argument("--offset", action="append", default=[], metavar="S|auto",
                   help="seconds after the first session's start that the "
                        "next session starts (negative: before it); one per "
                        "session after the first, in order, missing ones 0. "
                        "`auto` lines its strokes up with the first's")
    p.add_argument("--search", type=float, default=DEFAULT_SEARCH_S,
                   help="--offset auto looks within this many seconds of 0")
    p.add_argument("--dry-run", action="store_true",
                   help="print the crew, the offsets and the consumer "
                        "settings, then exit without sending anything")
    add_playback_args(p)
    a = p.parse_args(argv)
    finish_playback_args(p, a)
    if len(a.sessions) < 2:
        p.error("give two or more sessions (one plays with recorder.play)")
    if a.search <= 0:
        p.error("--search must be positive")
    try:
        parse_offsets(a.offset, len(a.sessions))
    except ValueError as e:
        p.error(f"--offset: {e}")
    return a


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: engine.STOP.set())
    sessions: list[Session] = []
    trks: list[Track] = []
    try:
        try:
            sessions = [Session(s) for s in args.sessions]
        except FileNotFoundError as e:
            raise SystemExit(f"[mix] {e}") from None
        members = crew(sessions, args)
        trks = tracks(members, args)
        describe(members, trks, args)
        if args.dry_run:
            return 0
        engine.run(trks, args)
    finally:
        for t in trks:
            t.close()
        for s in sessions:
            s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
