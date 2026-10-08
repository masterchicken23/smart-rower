#!/usr/bin/env python3
"""
Per-rower endpoints, under /rower/{seat}/.

Seats are 1-based with bow = 1, following rowing convention rather than array
indexing. The seat number is a position in the boat, not an athlete identity:
sensor firmware is configured with its seat, and who is sitting there is a
separate concern the funnel does not track.

Nothing here is configured per crew. A seat exists because something published
to rower/<seat>/..., so the same service handles a single and an eight with no
change.
"""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Query, Request

from ..compute import seat_position
from ..models import (
    AprilTagResponse,
    RawResponse,
    RowerSummary,
    SeatPositionResponse,
    StreamKey,
)
from ..sources.zmq_apriltag import STREAM as APRILTAG
from ..store import is_stale
from . import deps

router = APIRouter(tags=["rower"])

SEAT_POSITION = seat_position.OUTPUT


@router.get("/rowers", response_model=list[RowerSummary],
            summary="Seats discovered so far")
async def rowers(request: Request):
    s = deps.get_settings(request)
    store = deps.get_store(request)
    snap = deps.get_snapshot(request)
    now = time.monotonic()

    out = []
    for seat in store.seats():
        keys = store.streams_for("rower", seat)
        ages = [a for a in (store.buffers[k].age_ms(now) for k in keys)
                if a is not None]
        age = min(ages) if ages else None
        presence = store.presence.get(seat) or {}
        out.append({
            "seat": seat,
            "streams": [k.stream for k in keys],
            "age_ms": None if age is None else round(age, 1),
            "stale": is_stale(age, s.max_age_ms),
            "up": presence.get("up"),
        })
    return deps.send(out, snap=snap)


@router.get("/rower/{seat}", summary="Every computed block for one seat")
async def rower(seat: int, request: Request):
    """404 if the seat has never published anything -- distinct from a known
    seat whose sensor has gone quiet, which is a 503. 503 here means every one
    of the seat's streams is stale; if any is fresh the response is 200 and the
    per-stream `stale` flags say which to trust."""
    snap = deps.get_snapshot(request)
    blocks = snap.rowers.get(seat)
    if blocks is None:
        return deps.send(
            {"error": "not found",
             "detail": f"seat {seat} has not published; see /rowers"},
            404, snap=snap, state="no-data")
    if blocks and all(b.get("stale") for b in blocks.values()):
        ages = [b["age_ms"] for b in blocks.values()
                if b.get("age_ms") is not None]
        return deps.send({}, 503, snap=snap, state="stale",
                         age_ms=min(ages) if ages else None)
    return deps.send({"seat": seat, "streams": blocks}, snap=snap, state="ok")


@router.get("/rower/{seat}/position", response_model=SeatPositionResponse,
            summary="Derived seat position (the app's seat endpoint)")
async def rower_position(seat: int, request: Request):
    """Seat position, as processed rather than as measured.

    This is the endpoint a client should display. It is the output of the
    transformation in `funnel/compute/seat_position.py` -- spike rejection,
    low-pass filtering, and normalisation to 0.0 at the catch and 1.0 at the
    finish against endpoints learned from the rower's own strokes -- not the
    sensor's raw millimetres, which are specific to where that sensor happens
    to be bolted.

    `position` is null until the seat has seen one full stroke (`calibrated`
    is false until then). A client should treat a null `position` as "no
    value", exactly as it would treat a stale reading. `velocity_mms` is
    positive on the drive.

    The underlying raw stream remains available at `/rower/{seat}/seat` and
    `/rower/{seat}/raw` for debugging.
    """
    snap = deps.get_snapshot(request)
    blocks = snap.rowers.get(seat)
    if blocks is None:
        return deps.send(
            {"error": "not found",
             "detail": f"seat {seat} has not published; see /rowers"},
            404, snap=snap, state="no-data")
    block: Optional[dict] = blocks.get(SEAT_POSITION)
    return deps.block_response(
        block, snap,
        f"seat {seat} has no {SEAT_POSITION} data; it has published no "
        f"'{seat_position.RAW_STREAM}' stream")


@router.get("/rower/{seat}/apriltag", response_model=AprilTagResponse,
            summary="Newest raw AprilTag record from this seat's camera")
async def rower_apriltag(seat: int, request: Request):
    """The detector's record for the latest frame from the camera assigned to
    this seat (FUNNEL_APRILTAG_CAMERAS), passed through untouched under
    `record`: tag pose in the camera frame, pixel corners, decode quality and
    the frame's timing.

    404 if the seat, or its camera, has never published; 503 with an empty
    body once the newest record is older than FUNNEL_MAX_AGE_MS. A detector
    sends a record for every frame, tags or not (`n: 0`), so a 503 means the
    detector or its camera is down, not that no tag is in view.

    Recent records are at `/rower/{seat}/raw?stream=apriltag`.
    """
    snap = deps.get_snapshot(request)
    blocks = snap.rowers.get(seat)
    if blocks is None:
        return deps.send(
            {"error": "not found",
             "detail": f"seat {seat} has not published; see /rowers"},
            404, snap=snap, state="no-data")
    block: Optional[dict] = blocks.get(APRILTAG)
    resp = deps.block_response(
        block, snap,
        f"seat {seat} has no AprilTag camera data; check "
        f"FUNNEL_APRILTAG_CAMERAS and the 'apriltag' source in /health")
    if resp.status_code != 200 or block is None:
        return resp
    body = {
        "seat": seat,
        "cam": block.get("dev"),
        "age_ms": block.get("age_ms"),
        "rate_hz": block.get("rate_hz"),
        "stale": block.get("stale", True),
        "n_gap": block.get("n_gap", 0),
        "tick": block.get("seq"),
        "t": block.get("t"),
        "record": block.get("values") or {},
    }
    return deps.send(body, snap=snap, state="ok", age_ms=block.get("age_ms"))


@router.get("/rower/{seat}/apriltag/meta",
            summary="Camera model and conventions for this seat's camera")
async def rower_apriltag_meta(seat: int, request: Request):
    """The detector's latest `apriltag_meta` record for this seat's camera:
    intrinsics of the corrected image (`K_rect`, which `center_px` and
    `corners_px` are in), the calibration, tag sizes and frame conventions.
    Resent every ~2 s, so it is never 503'd for age -- it describes the camera
    rather than measuring anything -- but `age_ms` says how recently it was
    confirmed."""
    snap = deps.get_snapshot(request)
    rec = deps.get_store(request).apriltag_meta.get(seat)
    if rec is None:
        return deps.send(
            {"error": "not found",
             "detail": f"no apriltag_meta from seat {seat}'s camera yet"},
            404, snap=snap, state="no-data")
    age = (time.monotonic() - rec["t_recv"]) * 1000.0
    body = {k: v for k, v in rec.items() if k != "t_recv"}
    return deps.send({"seat": seat, "age_ms": round(age, 1), "meta": body},
                     snap=snap, state="ok", age_ms=age)


@router.get("/rower/{seat}/raw", response_model=RawResponse,
            summary="Recent raw samples for one seat (debug)")
async def rower_raw(seat: int, request: Request,
                    stream: str = Query("seat", description="which stream"),
                    window_s: float = Query(
                        2.0, gt=0, le=60,
                        description="how far back to read")):
    """A slice of the ring buffer, for debugging a sensor. Still a pure lookup
    -- it reads buffered samples and does not compute anything -- but it is
    unbounded in a way the other endpoints are not, so the window is capped."""
    s = deps.get_settings(request)
    store = deps.get_store(request)
    snap = deps.get_snapshot(request)
    key = StreamKey("rower", seat, stream)
    buf = store.get(key)
    if buf is None:
        return deps.send(
            {"error": "not found", "detail": f"no stream {key.as_path()}"},
            404, snap=snap, state="no-data")
    now = time.monotonic()
    samples = buf.window(min(window_s, s.buffer_seconds), now)
    body = {
        "path": key.as_path(),
        "window_s": window_s,
        "n": len(samples),
        "samples": [{
            "age_ms": round((now - x.t_recv) * 1000.0, 1),
            "t": None if x.t_src is None else round(x.t_src, 4),
            "seq": x.seq,
            "values": x.values,
        } for x in samples],
    }
    return deps.send(body, snap=snap, state="ok", age_ms=buf.age_ms(now))


@router.get("/rower/{seat}/{stream}", summary="One stream for one seat")
async def rower_stream(seat: int, stream: str, request: Request):
    snap = deps.get_snapshot(request)
    blocks = snap.rowers.get(seat) or {}
    block: Optional[dict] = blocks.get(stream)
    return deps.block_response(
        block, snap,
        f"seat {seat} has no stream {stream!r}; see /rower/{seat}")
