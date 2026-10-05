#!/usr/bin/env python3
"""
Boat-level endpoints, under /boat/.

Anything not attributable to a single rower lives here: the boat's own IMU,
speed and heading, the frame-level output of the vision process, and any
crew-wide derived value (a synchrony score across seats belongs to the boat,
not to any one seat in it).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request

from . import deps

router = APIRouter(prefix="/boat", tags=["boat"])


@router.get("", summary="Every boat-level computed block")
@router.get("/", include_in_schema=False)
async def boat(request: Request):
    snap = deps.get_snapshot(request)
    return deps.send({"streams": snap.boat}, snap=snap,
                     state="ok" if snap.boat else "no-data")


@router.get("/{stream}", summary="One boat-level stream")
async def boat_stream(stream: str, request: Request):
    snap = deps.get_snapshot(request)
    block: Optional[dict] = snap.boat.get(stream)
    return deps.block_response(
        block, snap, f"no boat stream {stream!r}; see /boat")
