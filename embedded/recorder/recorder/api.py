#!/usr/bin/env python3
"""
The HTTP control surface: start, stop, and look.

It lives on its own port (REC_HTTP_PORT, 8010) and every route is under
/recorder, docs included, so it cannot collide with the funnel's API on 8000
-- not even if both are ever put behind one proxy. Unlike the funnel this API
changes state, which is why start and stop are POSTs.

Handlers are plain `def`, so FastAPI runs them in its threadpool: a stop
drains the write queue and fsyncs, which must not stall the event loop.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__, log, taps
from .config import Settings, load
from .session import BusyError, IdleError, LowDiskError, SessionManager

PREFIX = "/recorder"

DESCRIPTION = """
Raw, time-synchronised recording of what the Jetson ingests: every MQTT
message, every camera frame (from april-detect's raw frame tee), every
AprilTag record. One session at a time; each is a directory named after its
UTC start time. Play one back with `python -m recorder.play <session>`.
"""

router = APIRouter(prefix=PREFIX, tags=["recorder"])


def _mgr(request: Request) -> SessionManager:
    return request.app.state.manager


def _err(status: int, error: str, detail: str) -> JSONResponse:
    return JSONResponse({"error": error, "detail": detail}, status_code=status)


@router.get("", summary="What this service is and what it serves")
def index() -> dict[str, Any]:
    return {
        "service": "smart-rower recorder",
        "version": __version__,
        "endpoints": {
            f"POST {PREFIX}/start?label=": "start a session (409 if one is running)",
            f"POST {PREFIX}/stop": "stop it; returns the session summary",
            f"GET {PREFIX}/status": "live sources, and the session if recording",
            f"GET {PREFIX}/sessions": "recorded sessions, newest first",
            f"GET {PREFIX}/livez": "process liveness (container healthcheck)",
            f"GET {PREFIX}/docs": "interactive API docs",
        },
    }


@router.get("/livez", summary="Process liveness only")
def livez(request: Request) -> dict[str, Any]:
    return {"ok": True, "recording": _mgr(request).recording}


@router.get("/status", summary="Sources, rates and the current session")
def status(request: Request) -> dict[str, Any]:
    """Every source's live rate, age and sequence gaps, whether or not a
    session is recording, so a dead camera is visible *before* you start."""
    return _mgr(request).status()


@router.post("/start", summary="Start a session")
def start(request: Request,
          label: str = Query("", max_length=64,
                             description="appended to the directory name, "
                                         "e.g. `pause-drill`")):
    try:
        return _mgr(request).start(label)
    except BusyError as e:
        return _err(409, "already recording", f"session {e} is running; "
                    f"POST {PREFIX}/stop first")
    except LowDiskError as e:
        return _err(507, "insufficient storage", str(e))


@router.post("/stop", summary="Stop the session and finalise its files")
def stop(request: Request):
    try:
        return _mgr(request).stop("request")
    except IdleError:
        return _err(409, "not recording", f"POST {PREFIX}/start first")


@router.get("/sessions", summary="Recorded sessions, newest first")
def sessions(request: Request) -> list[dict[str, Any]]:
    return _mgr(request).sessions()


async def _stats(mgr: SessionManager, period: float) -> None:
    """The repo's stderr stats line, every REC_STATS_INTERVAL_S."""
    while True:
        await asyncio.sleep(period)
        st = mgr.status()
        src = st["sources"]
        bits = []
        if "mqtt" in src:
            devs = src["mqtt"]["devices"]
            rate = sum(d["rate_hz"] or 0 for d in devs.values()
                       if (d["age_ms"] or 1e9) < 2000)
            bits.append(f"mqtt {'up' if src['mqtt']['connected'] else 'DOWN'} "
                        f"{len(devs)} topics {rate:.0f}/s")
        for cam, v in src["video"].items():
            fresh = v.get("age_ms") is not None and v["age_ms"] < 2000
            bits.append(f"{cam} {v.get('rate_hz') or 0:.1f}fps" if fresh
                        else f"{cam} --")
        if "apriltag" in src:
            bits.append(f"apriltag {len(src['apriltag']['devices'])} dev")
        s = st["session"]
        rec = (f"REC {s['name']} {s['elapsed_s']:.0f}s {s['written_mb']:.0f}MB "
               f"q {s['queued_mb']:.1f}MB drop {sum(s['dropped'].values())}"
               if s else "idle")
        log.info(f"[stats] {rec} | {' | '.join(bits)} | free "
                 f"{st['disk_free_gb']} GB")


def create_app(settings: Optional[Settings] = None, *,
               start_taps: bool = True) -> FastAPI:
    """`start_taps=False` builds the app with no sockets, for tests; items can
    then be handed to `app.state.manager.offer()` directly."""
    settings = settings or load()
    log.setup(settings.log_level)
    manager = SessionManager(settings)
    mqtt, all_taps = taps.build(settings, manager.offer) if start_taps else (None, [])
    manager.attach(mqtt, all_taps)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for t in all_taps:
            t.start()
        if settings.autostart:
            await asyncio.sleep(1.0)        # let sockets join before recording
            try:
                manager.start("auto")
            except (BusyError, LowDiskError) as e:
                log.error(f"[rec] autostart failed: {e}")
        task = (asyncio.create_task(_stats(manager, settings.stats_interval_s))
                if settings.stats_interval_s > 0 else None)
        log.info(f"[http] control on {settings.http_host}:{settings.http_port}"
                 f"{PREFIX} (no authentication)")
        st = manager.storage
        log.info(f"[storage] {st['dir']} is on {st['fs'] or 'an unknown filesystem'}")
        if st["warning"]:
            log.warn(f"[storage] WARNING: {st['warning']}")
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
            if manager.recording:
                # SIGTERM from `docker stop` lands here: finish the files.
                try:
                    await asyncio.to_thread(manager.stop, "shutdown")
                except IdleError:
                    pass
            for t in all_taps:
                t.stop()
            log.info("[main] stopped")

    app = FastAPI(title="smart-rower recorder", version=__version__,
                  description=DESCRIPTION, lifespan=lifespan,
                  docs_url=f"{PREFIX}/docs",
                  openapi_url=f"{PREFIX}/openapi.json", redoc_url=None)
    app.state.manager = manager
    app.state.settings = settings
    if settings.cors:
        app.add_middleware(CORSMiddleware, allow_origins=[settings.cors],
                           allow_methods=["GET", "POST", "OPTIONS"],
                           allow_headers=["*"])
    app.include_router(router)
    return app
