#!/usr/bin/env python3
"""
Service-level endpoints: discovery, liveness, health, and the whole-snapshot
read the mobile client actually uses.

Note the split between /livez and /health. /livez says only "the process is
up", and is what the container healthcheck probes. /health says "the data is
good", and answers 503 when nothing fresh is arriving. Pointing a container
healthcheck at /health would restart a perfectly healthy funnel every time the
boat's sensors were switched off.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request

from ..compute import seat_position
from ..models import HealthResponse, SnapshotResponse, StreamStats
from ..store import is_stale
from . import deps

router = APIRouter(tags=["system"])


@router.get("/", summary="What this service is and what it serves")
async def index(request: Request) -> dict[str, Any]:
    s = deps.get_settings(request)
    return {
        "service": "smart-rower funnel",
        "version": request.app.version,
        "endpoints": [
            "/snapshot", "/health", "/livez", "/streams",
            "/rowers", "/rower/{seat}", "/rower/{seat}/position",
            "/rower/{seat}/{stream}", "/rower/{seat}/raw",
            "/rower/{seat}/apriltag", "/rower/{seat}/apriltag/meta",
            "/boat", "/boat/{stream}", "/docs",
        ],
        "note": "every endpoint is a lookup of the last compute tick; "
                "nothing is computed per request",
        "seat_position": "/rower/{seat}/position is the derived value to "
                         "display; /rower/{seat}/seat is the raw stream",
        "compute_hz": s.compute_hz,
        "max_age_ms": s.max_age_ms,
    }


@router.get("/livez", summary="Process liveness only (container healthcheck)")
async def livez(request: Request) -> dict[str, Any]:
    state = deps.get_state(request)
    loop = deps.parts(request)["loop"]
    return {"alive": True, "uptime_s": round(state.uptime_s(), 1),
            "tick": loop.n_ticks}


@router.get("/health", response_model=HealthResponse,
            summary="Data health: 503 when nothing fresh is arriving")
async def health(request: Request):
    s = deps.get_settings(request)
    store = deps.get_store(request)
    state = deps.get_state(request)
    p = deps.parts(request)
    now = time.monotonic()

    stats = store.stats(now, s.max_age_ms)
    fresh = [x for x in stats if not x["stale"]]
    status = "ok" if fresh else ("stale" if stats else "no-data")

    recorder = p["recorder"]
    body = {
        "status": status,
        "uptime_s": round(state.uptime_s(), 1),
        "tick": p["loop"].stats(),
        "sources": {src.name: src.stats() for src in p["sources"]},
        "streams": stats,
        "max_age_ms": s.max_age_ms,
        "recording": None if recorder is None else recorder.stats()["session"],
        # Surfaced here so the state of the derived-value pipelines is visible
        # without having to read the source.
        "pipelines": {"seat_position": dict(seat_position.STAGES)},
    }
    if store.n_unknown_topic:
        body["unknown_topics"] = store.n_unknown_topic
    return deps.send(body, 200 if fresh else 503, snap=state.current,
                     state=status)


@router.get("/streams", response_model=list[StreamStats],
            summary="Every stream seen, with its rate and drop counters")
async def streams(request: Request):
    s = deps.get_settings(request)
    store = deps.get_store(request)
    return deps.send(store.stats(time.monotonic(), s.max_age_ms),
                     snap=deps.get_snapshot(request))


@router.get("/snapshot", response_model=SnapshotResponse,
            summary="Everything the last tick computed, in one response")
async def snapshot(request: Request):
    """The endpoint a dashboard should poll.

    Unlike the per-resource routes this always answers 200 while the service is
    running, and marks individual streams `stale` inside the payload. A crew of
    eight should cost one request per refresh, and one failed seat sensor
    should grey out one tile rather than failing the whole poll.
    """
    snap = deps.get_snapshot(request)
    s = deps.get_settings(request)
    now = time.monotonic()
    age = deps.snapshot_age_ms(snap, now)

    body = {
        "tick": snap.tick,
        "t": snap.t,
        "age_ms": 0.0 if age is None else round(age, 1),
        "rowers": snap.rowers,
        "boat": snap.boat,
        "streams": snap.streams,
        "presence": snap.presence,
    }
    # The snapshot itself going stale means the compute loop has stopped, which
    # is a different and more serious fault than a sensor going quiet.
    state = "starting" if snap.tick == 0 else (
        "stale-tick" if is_stale(age, max(s.max_age_ms * 5, 1000.0)) else "ok")
    return deps.send(body, 200, snap=snap, state=state, age_ms=age or 0.0)
