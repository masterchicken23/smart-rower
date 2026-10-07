#!/usr/bin/env python3
"""
Shared request-handling helpers.

The staleness convention is lifted from experiments/network-video-yolo/
pose_api.py and is worth restating, because it is a deliberate choice rather
than an accident: a reading older than FUNNEL_MAX_AGE_MS is served as `503`
with an empty body, not as a `200` with an old number in it. On a boat, a
number that stopped updating looks exactly like a number that is not changing,
and the second is a legitimate reading. The status code removes the ambiguity.

Diagnostics ride in `X-Funnel-*` headers so the body stays the clean contract.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from ..config import Settings
from ..models import Snapshot
from ..store import StateStore, StreamStore

NO_STORE = {"Cache-Control": "no-store"}


def parts(request: Request) -> dict[str, Any]:
    return request.app.state.parts


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> StreamStore:
    return parts(request)["store"]


def get_state(request: Request) -> StateStore:
    return parts(request)["state"]


def get_snapshot(request: Request) -> Snapshot:
    return parts(request)["state"].current


def snapshot_age_ms(snap: Snapshot, now: float) -> Optional[float]:
    """How long ago the serving snapshot was computed. None before the first
    tick, which is how a client tells "starting up" from "running"."""
    if snap.tick == 0:
        return None
    return (now - snap.t_recv) * 1000.0


def send(body: Any, status: int = 200, *, snap: Optional[Snapshot] = None,
         state: Optional[str] = None, age_ms: Optional[float] = None,
         extra: Optional[dict[str, str]] = None) -> JSONResponse:
    headers = dict(NO_STORE)
    if state is not None:
        headers["X-Funnel-Status"] = state
    if age_ms is not None:
        headers["X-Funnel-Age-Ms"] = str(round(age_ms, 1))
    if snap is not None:
        headers["X-Funnel-Tick"] = str(snap.tick)
    if extra:
        headers.update(extra)
    return JSONResponse(body, status_code=status, headers=headers)


def block_response(block: Optional[dict[str, Any]], snap: Snapshot,
                   not_found_detail: str) -> JSONResponse:
    """The per-resource convention, in one place.

      unknown stream  -> 404, it will never exist until something publishes
      never published -> 503 "no-data"
      too old         -> 503 "stale", empty body
      otherwise       -> 200 with the block
    """
    if block is None:
        return send({"error": "not found", "detail": not_found_detail},
                    404, snap=snap, state="no-data")
    age = block.get("age_ms")
    if age is None:
        return send({}, 503, snap=snap, state="no-data")
    if block.get("stale"):
        return send({}, 503, snap=snap, state="stale", age_ms=age)
    return send(block, 200, snap=snap, state="ok", age_ms=age)
