#!/usr/bin/env python3
"""
Entry point: `python -m recorder`.

uvicorn owns the event loop and the signals; SIGTERM from `docker stop` runs
the lifespan shutdown in api.py, which finishes any session in progress (the
compose file allows it 30 s). Configuration is REC_* environment variables.

The tools in this package are separate entry points with argparse flags:

    python -m recorder.report <session>
    python -m recorder.play   <session>
"""

from __future__ import annotations

import sys

from . import log
from .config import load

APP = "recorder.api:create_app"


def main() -> int:
    s = load()
    log.setup(s.log_level)

    import uvicorn

    log.info(f"[main] recorder starting: dir={s.dir} mqtt={s.mqtt_host}:"
             f"{s.mqtt_port} cameras={','.join(s.camera_map) or '-'} "
             f"apriltag={len(s.apriltag_endpoints)} endpoint(s)")
    # One worker: the taps and the session are in-process state.
    uvicorn.run(APP, factory=True, host=s.http_host, port=s.http_port,
                workers=1, log_config=None, access_log=s.access_log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
