#!/usr/bin/env python3
"""
The FastAPI application, and the lifespan that wires the service together.

Everything lives on one event loop: ingest tasks, the compute loop, the
recorder and the request handlers. That is what makes the no-locks invariant in
store.py hold, and it is why nothing here may block.

Request handling never computes. Every endpoint reads the Snapshot that the
last compute tick published, so a client polling at 1 Hz and a client polling
at 100 Hz cost the same and neither can disturb ingest or the tick rate.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .. import log
from ..compute.loop import make_loop
from ..config import Settings, load
from ..recorder import Recorder
from ..sources.mqtt import MqttSource
from ..store import StateStore, StreamStore
from . import routes_boat, routes_rower, routes_system

DESCRIPTION = """
Read-only view of the live smart-rower sensor data.

Sensors publish to MQTT at whatever rate they like; the funnel buffers them and
recomputes derived values on a fixed grid. Every endpoint here is a lookup of
the most recent result -- no request triggers ingest, computation or a fetch,
and request rate is unrelated to data rate.

Per-resource endpoints answer `503` when their data is older than
`FUNNEL_MAX_AGE_MS`, rather than presenting a stale reading as current.
`/snapshot` instead answers `200` with per-stream `stale` flags, so one dead
sensor greys out one tile instead of failing a dashboard's whole poll.
"""


def build(settings: Settings) -> dict[str, Any]:
    """Construct the service's parts without starting anything."""
    store = StreamStore(maxlen=settings.buffer_max_samples,
                        window_s=settings.buffer_seconds)
    state = StateStore()

    recorder: Optional[Recorder] = None
    if settings.record_enabled:
        # Only constructed when recording is on. Off, the cost to ingest is a
        # single `is not None` test per sample.
        recorder = Recorder(settings.record_dir, settings.record_queue,
                            settings.record_flush_s)

    sources: list = []
    if settings.replay_path:
        from ..sources.replay import ReplaySource
        sources.append(ReplaySource(store, settings.replay_path,
                                    settings.replay_speed,
                                    settings.replay_loop, recorder))
    else:
        sources.append(MqttSource(store, settings, recorder))
    if settings.zmq_enabled:
        from ..sources.zmq_pose import ZmqPoseSource
        sources.append(ZmqPoseSource(store, settings, recorder))

    loop = make_loop(store, state, settings)
    return {"store": store, "state": state, "recorder": recorder,
            "sources": sources, "loop": loop, "settings": settings}


@asynccontextmanager
async def lifespan(app: FastAPI):
    parts = app.state.parts
    settings: Settings = parts["settings"]
    recorder: Optional[Recorder] = parts["recorder"]

    tasks: list[asyncio.Task] = []
    if recorder is not None:
        recorder.start(meta={"settings": settings.model_dump()})
        tasks.append(asyncio.create_task(recorder.run(), name="recorder"))
    for src in parts["sources"]:
        tasks.append(asyncio.create_task(src.run(), name=f"src:{src.name}"))
    tasks.append(asyncio.create_task(parts["loop"].run(), name="compute"))
    app.state.tasks = tasks

    log.info(f"[http] serving on {settings.http_host}:{settings.http_port} "
             f"(read-only, no authentication)")
    try:
        yield
    finally:
        parts["loop"].stop()
        for t in tasks:
            t.cancel()
        # Surface a task that died of its own accord (a NotImplementedError
        # from an unimplemented source, say) rather than exiting silently.
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for t, res in zip(tasks, results, strict=True):
            if isinstance(res, Exception) and not isinstance(
                    res, asyncio.CancelledError):
                log.error(f"[main] task {t.get_name()} ended with {res!r}")
        log.info("[main] stopped")


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load()
    log.setup(settings.log_level)

    app = FastAPI(
        title="smart-rower funnel",
        version="0.1.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.parts = build(settings)
    app.state.settings = settings

    if settings.cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[settings.cors],
            allow_methods=["GET", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["X-Funnel-Status", "X-Funnel-Age-Ms",
                            "X-Funnel-Tick"],
        )

    app.include_router(routes_system.router)
    app.include_router(routes_rower.router)
    app.include_router(routes_boat.router)
    return app
