#!/usr/bin/env python3
"""
Entry point: `python -m funnel`.

uvicorn owns the event loop and the signal handling; SIGTERM from `docker stop`
runs the lifespan shutdown in api/app.py, which cancels ingest, stops the
compute loop and flushes the recorder.

Configuration is entirely environment variables (FUNNEL_*). There are no
command-line flags, because the thing that configures this service is a compose
file. Run `python -c "from funnel.config import load; print(load())"` to see the
effective settings.
"""

from __future__ import annotations

import asyncio
import sys

from . import log
from .config import load

APP = "funnel.api.app:create_app"


def main(argv: list[str] | None = None) -> int:
    settings = load()
    log.setup(settings.log_level)

    import uvicorn

    log.info(f"[main] funnel starting: mqtt={settings.mqtt_host}:"
             f"{settings.mqtt_port} compute={settings.compute_hz}Hz "
             f"record={'on' if settings.record_enabled else 'off'}")

    opts = {
        "host": settings.http_host,
        "port": settings.http_port,
        "log_config": None,     # log.setup already routed logging to stderr
        "access_log": settings.access_log,
    }

    if sys.platform != "win32":
        # The deployment path. One process: the buffers are in-memory and
        # shared by reference, so a second worker would serve its own empty
        # copy of them.
        uvicorn.run(APP, factory=True, workers=1, **opts)
        return 0

    # Windows is a development-only path, and it needs the event loop chosen by
    # hand. uvicorn picks the Proactor loop on Windows, which does not
    # implement add_reader -- and that is exactly how paho (under aiomqtt)
    # watches its socket. The failure is quiet and misleading: the broker
    # connection just times out, with add_reader tracebacks in the log, and the
    # funnel sits serving 503s as though no sensor were publishing. Setting the
    # policy is not enough because uvicorn overrides it, so the loop is created
    # here and uvicorn told (loop="none") to use the running one.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    config = uvicorn.Config(APP, factory=True, loop="none", **opts)
    asyncio.run(uvicorn.Server(config).serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
